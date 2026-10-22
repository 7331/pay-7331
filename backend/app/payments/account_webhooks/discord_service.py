"""AccountDiscordWebhookService: an account's Discord channel webhook for its invoice news
(PLAN.md section 8.4).

- One per account, and one account per Discord webhook (Discord's rate limits are per webhook, so two accounts
  sharing one could not be kept to them). Setting or replacing it needs a fresh passkey check, as the webhook URL does:
  it decides where the account's payment news goes, so a stolen session must not point it elsewhere. Only the URL's
  webhook id and sealed token are kept (discord_webhook_urls.py, secret_encryption.py); the URL is never answered or
  logged. Replacing it drops the old one's hold, so the account's waiting messages go to the new one now. Removing
  needs only the session.
- Every invoice event of the account (the same ``invoice.*`` and ``refund.*`` account events the webhook gets) and
  every ``operator.alert`` (only the operator's account gets those; 2026-10-13, so alerts reach the owner's Discord)
  made after it was first set is queued once as a Discord message (``queue_claimed_account_events``, called in the
  delivery loop's claim beside the webhook's), into the same deliveries table with ``receiver_kind`` ``discord``,
  its body fixed when queued (discord_messages.py). "Send test message" queues ``discord.test``.
- A pasted URL's ``?thread_id=`` is kept and every message goes to that thread (a forum or media channel's webhook
  takes messages only into a thread; discord_webhook_urls.py).
- Once Discord answered 404, Unknown Webhook or Invalid Webhook Token (discord_delivery.py) the webhook is marked
  deleted in Discord: nothing more is queued or sent to it, and /settings says so, until the account sets one again.
"""

from datetime import UTC, datetime
from typing import Self

from pydantic import JsonValue
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import read_application_settings
from app.payments.account_events.enums import AccountEventType
from app.payments.account_events.types import ClaimedAccountEvent
from app.payments.account_webhooks import log_events
from app.payments.account_webhooks.discord_messages import (
    DISCORD_TEST_MESSAGE_EVENT_TYPE,
    build_discord_invoice_event_content,
    build_discord_message_body,
    build_discord_operator_alert_content,
    build_discord_test_message_body,
)
from app.payments.account_webhooks.discord_webhook_urls import DiscordWebhookAddress, parse_discord_webhook_url
from app.payments.account_webhooks.enums import (
    AccountDiscordWebhookStatus,
    AccountWebhookFailureReason,
    AccountWebhookReceiverKind,
)
from app.payments.account_webhooks.errors import (
    AccountDiscordWebhookInUseError,
    AccountDiscordWebhookNotSetError,
    AccountReceivesNoOperatorAlertsError,
)
from app.payments.account_webhooks.repository import AccountWebhookRepository
from app.payments.account_webhooks.schemas import AccountDiscordWebhookResponse, AccountWebhookDeliveryResponse
from app.payments.account_webhooks.secret_encryption import (
    derive_discord_webhook_token_encryption_key,
    seal_webhook_secret,
)
from app.payments.account_webhooks.service import TEST_MESSAGE_ID_PREFIX, present_delivery
from app.payments.accounts import log_events as account_log_events
from app.payments.accounts.enums import ChallengePurpose
from app.payments.accounts.service import AccountService
from app.payments.accounts.types import AccountAccess
from app.payments.invoices.configuration import read_invoice_settings
from app.shared import logging as log
from app.shared.errors import AppError
from app.shared.identifiers import generate_public_id

logger = log.get_logger(__name__)

DISCORD_MESSAGE_ID_PREFIX = "discord_"
DISCORD_WEBHOOK_ID_UNIQUE_INDEX = "uq_account_discord_webhooks_discord_webhook_id"


async def find_operator_alert_repeat_hours_on_session(session: AsyncSession, *, account_id: int) -> int | None:
    """The operator account's alert repeat, for the watcher's operator alert recorder in its own transaction. None: no
    Discord webhook, or one Discord deleted, so alerts are not repeated. A function, not a method: the recorder runs in
    the host's CLI too, and the service's constructor derives the token key it does not need."""
    return await AccountWebhookRepository(session=session).find_operator_alert_repeat_hours(account_id)


class AccountDiscordWebhookService:
    def __init__(
        self, *, repository: AccountWebhookRepository, account_service: AccountService, encryption_key: bytes
    ) -> None:
        self.repository = repository
        self.account_service = account_service
        self.encryption_key = encryption_key

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(
            repository=AccountWebhookRepository(session=session),
            account_service=AccountService.on_session(session),
            encryption_key=derive_discord_webhook_token_encryption_key(read_invoice_settings().invoice_salt_seed),
        )

    async def read_account_discord_webhook(self, account_id: int) -> AccountDiscordWebhookResponse:
        discord_webhook = await self.repository.find_discord_webhook(account_id)
        latest_messages = await self.repository.list_latest_account_deliveries(
            account_id, receiver_kind=AccountWebhookReceiverKind.DISCORD, limit=1
        )
        if discord_webhook is None:
            return AccountDiscordWebhookResponse(
                discord_webhook_id=None, status=None, deleted_in_discord_at=None, latest_message=None
            )
        operator_alert_repeat_hours = (
            discord_webhook.operator_alert_repeat_hours if self.is_operator_account(account_id) else None
        )
        return AccountDiscordWebhookResponse(
            discord_webhook_id=discord_webhook.discord_webhook_id,
            discord_thread_id=discord_webhook.discord_thread_id,
            status=(
                AccountDiscordWebhookStatus.DELETED_IN_DISCORD
                if discord_webhook.deleted_in_discord_at is not None
                else AccountDiscordWebhookStatus.ACTIVE
            ),
            deleted_in_discord_at=discord_webhook.deleted_in_discord_at,
            latest_message=present_delivery(latest_messages[0]) if latest_messages else None,
            operator_alert_repeat_hours=operator_alert_repeat_hours,
        )

    def is_operator_account(self, account_id: int) -> bool:
        """Whether operator alerts go to this account: the first of ``PAY_OPERATOR_ACCOUNT_IDS``. ``account_id`` is a
        signed-in account's, so it exists."""
        return account_id == read_invoice_settings().operator_alert_account_id

    async def set_operator_alert_repeat_hours(
        self, account_access: AccountAccess, *, repeat_hours: int
    ) -> AccountDiscordWebhookResponse:
        account_id = account_access.account_id
        if not self.is_operator_account(account_id):
            raise AccountReceivesNoOperatorAlertsError("only the operator's account gets operator alerts")
        if not await self.repository.update_operator_alert_repeat_hours(account_id, repeat_hours=repeat_hours):
            raise AccountDiscordWebhookNotSetError("set a Discord webhook first")
        log.emit(
            logger,
            event=log_events.AccountDiscordOperatorAlertRepeatSet(account_id=account_id, repeat_hours=repeat_hours),
        )
        return await self.read_account_discord_webhook(account_id)

    async def create_change_options(self, account_access: AccountAccess) -> dict[str, JsonValue]:
        return await self.account_service.create_fresh_passkey_check_options(
            account_access, purpose=ChallengePurpose.CHANGE_DISCORD_WEBHOOK
        )

    async def verify_discord_webhook_change(
        self, account_access: AccountAccess, *, url: str, credential_json: dict[str, JsonValue]
    ) -> DiscordWebhookAddress:
        """Checks the URL, then the passkey; answers the webhook's id, token and thread id for ``store_account_discord_webhook``
        (the router counts the day's changes in between, so a refused one does not count)."""
        discord_webhook_address = parse_discord_webhook_url(url)
        try:
            await self.account_service.verify_fresh_passkey_check(
                account_access, purpose=ChallengePurpose.CHANGE_DISCORD_WEBHOOK, credential_json=credential_json
            )
        except AppError as refusal:
            log.emit(
                logger,
                event=account_log_events.PasskeyCeremonyRefused(ceremony="change_discord_webhook", reason=refusal.code),
            )
            raise
        return discord_webhook_address

    async def store_account_discord_webhook(
        self, account_access: AccountAccess, *, discord_webhook_address: DiscordWebhookAddress
    ) -> AccountDiscordWebhookResponse:
        """Keeps the webhook's id and sealed token; refused when another account has that webhook. Replacing another
        one makes the account's held messages due now (repository.py)."""
        account_id = account_access.account_id
        # Same-account changes are serialized by the account row the passkey check locked.
        replaced_discord_webhook_id = await self.repository.find_discord_webhook_id(account_id)
        try:
            await self.repository.upsert_discord_webhook(
                account_id,
                discord_webhook_id=discord_webhook_address.discord_webhook_id,
                encrypted_discord_webhook_token=seal_webhook_secret(
                    discord_webhook_address.discord_webhook_token,
                    account_id=account_id,
                    encryption_key=self.encryption_key,
                ),
                discord_thread_id=discord_webhook_address.discord_thread_id,
            )
        except IntegrityError as conflict:
            if DISCORD_WEBHOOK_ID_UNIQUE_INDEX not in str(conflict.orig):
                raise
            raise AccountDiscordWebhookInUseError("another account already uses this Discord webhook") from conflict
        if (
            replaced_discord_webhook_id is not None
            and replaced_discord_webhook_id != discord_webhook_address.discord_webhook_id
        ):
            await self.repository.make_held_discord_deliveries_due_now(account_id)
        log.emit(logger, event=log_events.AccountDiscordWebhookSet(account_id=account_id))
        return await self.read_account_discord_webhook(account_id)

    async def remove_account_discord_webhook(self, account_access: AccountAccess) -> None:
        account_id = account_access.account_id
        if await self.repository.delete_discord_webhook(account_id):
            await self.repository.fail_pending_account_deliveries(
                account_id,
                receiver_kinds=[AccountWebhookReceiverKind.DISCORD],
                reason=AccountWebhookFailureReason.NO_WEBHOOK,
            )
            log.emit(logger, event=log_events.AccountDiscordWebhookRemoved(account_id=account_id))

    async def queue_test_message(self, account_access: AccountAccess) -> AccountWebhookDeliveryResponse:
        account_id = account_access.account_id
        # Locked for share, as queuing does: a removal committing meanwhile would otherwise leave this pending forever.
        discord_webhooks = await self.repository.list_discord_webhooks_of_accounts([account_id], lock_for_share=True)
        discord_webhook = discord_webhooks[0] if discord_webhooks else None
        if discord_webhook is None or discord_webhook.deleted_in_discord_at is not None:
            raise AccountDiscordWebhookNotSetError("set a Discord webhook URL first")
        delivery = await self.repository.insert_delivery(
            account_id,
            receiver_kind=AccountWebhookReceiverKind.DISCORD,
            account_event_id=None,
            webhook_message_id=generate_public_id(f"{DISCORD_MESSAGE_ID_PREFIX}{TEST_MESSAGE_ID_PREFIX}"),
            event_type=DISCORD_TEST_MESSAGE_EVENT_TYPE,
            body=build_discord_test_message_body(),
            due_at=datetime.now(UTC),
        )
        if delivery is None:
            raise RuntimeError("a fresh test message id collided")
        log.emit(
            logger,
            event=log_events.AccountWebhookDeliveryQueued(
                account_id=account_id, delivery_id=delivery.id, event_type=delivery.event_type, cause="test"
            ),
        )
        return present_delivery(delivery)

    async def queue_claimed_account_events(self, account_events: list[ClaimedAccountEvent]) -> None:
        """Queues each invoice event and operator alert whose account has a Discord webhook set before it and not
        deleted in Discord; other account events (``account.payout_address_changed``) are not sent."""
        sent_account_events = [
            account_event
            for account_event in account_events
            if account_event.account_event_response.invoice_id is not None
            or account_event.account_event_response.type == AccountEventType.OPERATOR_ALERT
        ]
        discord_webhooks_by_account_id = {
            discord_webhook.account_id: discord_webhook
            for discord_webhook in await self.repository.list_discord_webhooks_of_accounts(
                sorted({account_event.account_id for account_event in sent_account_events}), lock_for_share=True
            )
        }
        now = datetime.now(UTC)
        for account_event in sent_account_events:
            discord_webhook = discord_webhooks_by_account_id.get(account_event.account_id)
            if (
                discord_webhook is None
                or discord_webhook.deleted_in_discord_at is not None
                or account_event.account_event_response.created_at < discord_webhook.created_at
            ):
                continue
            content = (
                build_discord_operator_alert_content(
                    account_event.account_event_response,
                    repeat_hours=discord_webhook.operator_alert_repeat_hours,
                    activity_url=f"{read_application_settings().public_base_url.rstrip('/')}/activity",
                )
                if account_event.account_event_response.type == AccountEventType.OPERATOR_ALERT
                else build_discord_invoice_event_content(account_event.account_event_response)
            )
            delivery = await self.repository.insert_delivery(
                account_event.account_id,
                receiver_kind=AccountWebhookReceiverKind.DISCORD,
                account_event_id=account_event.account_event_id,
                webhook_message_id=f"{DISCORD_MESSAGE_ID_PREFIX}{account_event.account_event_response.id}",
                event_type=account_event.account_event_response.type,
                body=build_discord_message_body(content),
                due_at=now,
            )
            if delivery is not None:
                log.emit(
                    logger,
                    event=log_events.AccountWebhookDeliveryQueued(
                        account_id=account_event.account_id,
                        delivery_id=delivery.id,
                        event_type=delivery.event_type,
                        cause="event",
                    ),
                )

    async def mark_deleted_in_discord(
        self,
        account_id: int,
        *,
        discord_webhook_id: str,
        discord_thread_id: str | None,
        response_status: int,
        deleted_at: datetime,
    ) -> None:
        """Discord answered 404, Unknown Webhook or Invalid Webhook Token for that webhook (and thread): stop it and
        fail its pending messages. Nothing when the account no longer has that webhook and thread (it set another
        since, where its messages now go, or removed it) or it is marked already."""
        if not await self.repository.mark_discord_webhook_deleted_in_discord(
            account_id,
            discord_webhook_id=discord_webhook_id,
            discord_thread_id=discord_thread_id,
            deleted_at=deleted_at,
        ):
            return
        await self.repository.fail_pending_account_deliveries(
            account_id,
            receiver_kinds=[AccountWebhookReceiverKind.DISCORD],
            reason=AccountWebhookFailureReason.DISCORD_WEBHOOK_DELETED,
        )
        log.emit(
            logger,
            event=log_events.AccountDiscordWebhookDeletedInDiscord(
                account_id=account_id, response_status=response_status
            ),
        )
