"""AccountWebhookService: an account's webhook for its own server (PLAN.md section
8.3), Standard Webhooks (https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md).

- One https URL per account. Setting it (or changing it) and rotating the secret need a fresh passkey check
  (``change_webhook``): either shows where invoice events go or a secret that signs them, so a stolen session must not
  do it, as with the payout address. The first set makes a ``whsec_`` secret and a rotation a new one, each shown
  once in that answer; the secret is stored sealed (secret_encryption.py). Removing needs only the session.
- Every invoice event of the account (the ``invoice.*`` and ``refund.*`` account events) and every static deposit
  address event (``static_address.*``, its ``data`` the address and its payment or sweep; PLAN.md section 17) made
  after the webhook was set is queued once, by ``queue_claimed_account_events`` (called in the delivery loop's claim, delivery_service.py);
  a test is queued by the settings button: ``webhook.test`` (``{"message"}``) or a sample invoice event of the type
  the account picks (sample_webhook_events.py, 2026-10-21), its body marked ``"test": true``. Each delivery's body is fixed when queued: ``{"type",
  "timestamp", "data"}`` with ``data`` the invoice exactly as ``GET /api/v1/invoices/{id}`` showed it to the account
  at the event. ``webhook-id`` is the account event id (``test_...`` for a test), the same on every attempt and resend.
- An account invoice, made with a token or a session, may name its own ``callback_url`` (2026-10-21, Cryptomus
  ``url_callback``): the invoice's events go there too, beside the account's webhook, signed with the same secret and
  sent with the same ``webhook-id`` (the account event id), so a receiver getting both dedupes them. The URL meets the webhook URL's
  rules and needs an account webhook, whose secret signs it (``check_invoice_callback_url``, 409
  ``webhook_not_set`` without one). A callback is queued while the account has a webhook, disabled or not (a 410
  from the account's URL says nothing about the callback's), and fails as ``no_webhook`` once the account removed it.
- A 410 Gone from the receiver disables the webhook (``disable_after_receiver_gone``): Standard Webhooks says the
  sender "should disable the webhook endpoint, and stop sending it messages"
  (https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md, "Delivery success and
  failure"). Its pending deliveries fail, nothing more is queued, test events and resends are refused (409
  ``webhook_disabled``), and /settings says so until the account sets the URL again.
"""

from datetime import UTC, datetime
import json
from typing import Self

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.account_events.types import ClaimedAccountEvent
from app.payments.account_events.webhook_event_scope import is_webhook_event
from app.payments.account_webhooks import log_events
from app.payments.account_webhooks.configuration import read_webhook_allow_private_hosts
from app.payments.account_webhooks.enums import AccountWebhookFailureReason, AccountWebhookReceiverKind
from app.payments.account_webhooks.errors import (
    AccountWebhookDeliveryNotFoundError,
    AccountWebhookDisabledError,
    AccountWebhookNotSetError,
)
from app.payments.account_webhooks.models import AccountWebhookDelivery
from app.payments.account_webhooks.receiver_addresses import split_webhook_url
from app.payments.account_webhooks.repository import AccountWebhookRepository
from app.payments.account_webhooks.sample_webhook_events import build_sample_event_data
from app.payments.account_webhooks.schemas import (
    AccountWebhookDeliveryResponse,
    AccountWebhookResponse,
    ChangedAccountWebhookResponse,
)
from app.payments.account_webhooks.secret_encryption import (
    derive_webhook_secret_encryption_key,
    generate_webhook_secret,
    seal_webhook_secret,
)
from app.payments.accounts import log_events as account_log_events
from app.payments.accounts.enums import ChallengePurpose
from app.payments.accounts.service import AccountService
from app.payments.accounts.types import AccountAccess
from app.payments.chains.service import ChainsService
from app.payments.invoices.configuration import read_invoice_settings
from app.shared import logging as log
from app.shared.errors import AppError, InvalidInputError
from app.shared.identifiers import generate_public_id
from app.shared.timestamps import format_utc_timestamp

logger = log.get_logger(__name__)

# An invoice callback's stored message id: the prefix and the account event's public id (unique beside the webhook's
# own row).
INVOICE_CALLBACK_MESSAGE_ID_PREFIX = "callback_"
# A test message's webhook-id starts with it (the docs promise that marker); the rest is random, as a public id.
TEST_MESSAGE_ID_PREFIX = "test_"
LISTED_DELIVERY_COUNT = 20


def build_webhook_body(event_type: str, *, occurred_at: datetime, data: JsonValue, is_test: bool = False) -> str:
    """The exact text sent and signed: compact JSON, UTF-8, keys in the spec's order; a test adds ``"test": true``
    (a real event has no ``test`` key, so its body is as before 2026-10-21)."""
    body: dict[str, JsonValue] = {"type": event_type, "timestamp": format_utc_timestamp(occurred_at), "data": data}
    if is_test:
        body["test"] = True
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def present_delivery(delivery: AccountWebhookDelivery) -> AccountWebhookDeliveryResponse:
    return AccountWebhookDeliveryResponse(
        id=delivery.public_id,
        webhook_id=delivery.webhook_message_id,
        event_type=delivery.event_type,
        status=delivery.status,
        attempt_count=delivery.attempt_count,
        next_attempt_at=delivery.next_attempt_at,
        last_response_status=delivery.last_response_status,
        last_failure_reason=delivery.last_failure_reason,
        created_at=delivery.created_at,
    )


class AccountWebhookService:
    def __init__(
        self,
        *,
        repository: AccountWebhookRepository,
        account_service: AccountService,
        encryption_key: bytes,
        allow_private_hosts: bool,
    ) -> None:
        self.repository = repository
        self.account_service = account_service
        self.encryption_key = encryption_key
        self.allow_private_hosts = allow_private_hosts

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(
            repository=AccountWebhookRepository(session=session),
            account_service=AccountService.on_session(session),
            encryption_key=derive_webhook_secret_encryption_key(read_invoice_settings().invoice_salt_seed),
            allow_private_hosts=read_webhook_allow_private_hosts(),
        )

    async def read_account_webhook(self, account_id: int) -> AccountWebhookResponse:
        endpoint = await self.repository.find_endpoint(account_id)
        return AccountWebhookResponse(
            url=endpoint.url if endpoint is not None else None,
            disabled_at=endpoint.disabled_at if endpoint is not None else None,
            deliveries=[
                present_delivery(delivery)
                for delivery in await self.repository.list_latest_account_deliveries(
                    account_id, receiver_kind=AccountWebhookReceiverKind.STANDARD_WEBHOOK, limit=LISTED_DELIVERY_COUNT
                )
            ],
        )

    async def read_changed_account_webhook(
        self, account_id: int, *, new_secret: str | None
    ) -> ChangedAccountWebhookResponse:
        """The webhook after a change, with ``new_secret`` only in the answer that made it (the first set, a
        rotation)."""
        account_webhook = await self.read_account_webhook(account_id)
        return ChangedAccountWebhookResponse(
            url=account_webhook.url,
            disabled_at=account_webhook.disabled_at,
            deliveries=account_webhook.deliveries,
            secret=new_secret,
        )

    async def create_change_options(self, account_access: AccountAccess) -> dict[str, JsonValue]:
        return await self.account_service.create_fresh_passkey_check_options(
            account_access, purpose=ChallengePurpose.CHANGE_WEBHOOK
        )

    async def verify_change(self, account_access: AccountAccess, *, credential_json: dict[str, JsonValue]) -> None:
        try:
            await self.account_service.verify_fresh_passkey_check(
                account_access, purpose=ChallengePurpose.CHANGE_WEBHOOK, credential_json=credential_json
            )
        except AppError as refusal:
            log.emit(
                logger,
                event=account_log_events.PasskeyCeremonyRefused(ceremony="change_webhook", reason=refusal.code),
            )
            raise

    async def set_account_webhook(
        self, account_access: AccountAccess, *, url: str, credential_json: dict[str, JsonValue]
    ) -> ChangedAccountWebhookResponse:
        """Sets the URL once the passkey check verifies (turning a webhook a 410 disabled back on); the first set
        answers the new secret, once."""
        split_webhook_url(url, allow_private_hosts=self.allow_private_hosts)
        await self.verify_change(account_access, credential_json=credential_json)
        account_id = account_access.account_id
        endpoint = await self.repository.find_endpoint(account_id)
        new_secret = None
        if endpoint is None:
            new_secret = generate_webhook_secret()
            await self.repository.insert_endpoint(
                account_id,
                url=url,
                encrypted_secret=seal_webhook_secret(
                    new_secret, account_id=account_id, encryption_key=self.encryption_key
                ),
            )
        else:
            await self.repository.update_endpoint(account_id, url=url)
        log.emit(logger, event=log_events.AccountWebhookSet(account_id=account_id, new_secret=new_secret is not None))
        return await self.read_changed_account_webhook(
            account_id, new_secret=new_secret.get_secret_value() if new_secret else None
        )

    async def rotate_account_webhook_secret(
        self, account_access: AccountAccess, *, credential_json: dict[str, JsonValue]
    ) -> ChangedAccountWebhookResponse:
        """A new secret at once (the old one stops signing), answered once."""
        await self.verify_change(account_access, credential_json=credential_json)
        account_id = account_access.account_id
        if await self.repository.find_endpoint(account_id) is None:
            raise AccountWebhookNotSetError("set a webhook URL first")
        new_secret = generate_webhook_secret()
        await self.repository.update_endpoint(
            account_id,
            encrypted_secret=seal_webhook_secret(new_secret, account_id=account_id, encryption_key=self.encryption_key),
        )
        log.emit(logger, event=log_events.AccountWebhookSecretRotated(account_id=account_id))
        return await self.read_changed_account_webhook(account_id, new_secret=new_secret.get_secret_value())

    async def remove_account_webhook(self, account_access: AccountAccess) -> None:
        """The URL and its secret go; so do the pending deliveries signed with it, invoice callbacks included."""
        account_id = account_access.account_id
        if await self.repository.delete_endpoint(account_id):
            await self.repository.fail_pending_account_deliveries(
                account_id,
                receiver_kinds=[
                    AccountWebhookReceiverKind.STANDARD_WEBHOOK,
                    AccountWebhookReceiverKind.INVOICE_CALLBACK,
                ],
                reason=AccountWebhookFailureReason.NO_WEBHOOK,
            )
            log.emit(logger, event=log_events.AccountWebhookRemoved(account_id=account_id))

    async def disable_after_receiver_gone(self, account_id: int, *, url: str, disabled_at: datetime) -> None:
        """The receiver at ``url`` answered 410 Gone: disable the webhook and fail its pending deliveries. Nothing when
        the account has another URL since, or it is disabled already."""
        if not await self.repository.disable_endpoint(account_id, url=url, disabled_at=disabled_at):
            return
        await self.repository.fail_pending_account_deliveries(
            account_id,
            receiver_kinds=[AccountWebhookReceiverKind.STANDARD_WEBHOOK],
            reason=AccountWebhookFailureReason.RECEIVER_GONE,
        )
        log.emit(logger, event=log_events.AccountWebhookDisabledByReceiver(account_id=account_id))

    async def check_invoice_callback_url(self, account_id: int, *, callback_url: str) -> None:
        """An invoice's ``callback_url``: the webhook URL's rules (https, no user info or fragment, no address off the
        public internet), and an account webhook whose secret signs it."""
        try:
            split_webhook_url(callback_url, allow_private_hosts=self.allow_private_hosts)
        except InvalidInputError as refusal:
            raise InvalidInputError(refusal.message.replace("url:", "callback_url:", 1)) from refusal
        if await self.repository.find_endpoint(account_id) is None:
            raise AccountWebhookNotSetError(
                "callback_url: set the account webhook first; its secret signs the invoice's callbacks"
            )

    async def refuse_unless_webhook_sendable(self, account_id: int) -> None:
        """A test event or resend needs a webhook that a 410 has not disabled."""
        endpoint = await self.repository.find_endpoint(account_id)
        if endpoint is None:
            raise AccountWebhookNotSetError("set a webhook URL first")
        if endpoint.disabled_at is not None:
            raise AccountWebhookDisabledError("the receiver answered 410 Gone; set the URL again to turn it back on")

    async def queue_test_delivery(
        self,
        account_access: AccountAccess,
        *,
        event_type: str,
        chains_service: ChainsService,
        public_base_url: str,
    ) -> AccountWebhookDeliveryResponse:
        """``webhook.test`` with a message, or a sample invoice or customer address in the state ``event_type``
        leaves one in, signed like a real event and marked as a test (sample_webhook_events.py)."""
        account_id = account_access.account_id
        await self.refuse_unless_webhook_sendable(account_id)
        now = datetime.now(UTC)
        data = build_sample_event_data(
            event_type,
            chains_service=chains_service,
            public_base_url=public_base_url,
            destination_address=account_access.payout_address,
            now=now,
        )
        delivery = await self.repository.insert_delivery(
            account_id,
            receiver_kind=AccountWebhookReceiverKind.STANDARD_WEBHOOK,
            account_event_id=None,
            webhook_message_id=generate_public_id(TEST_MESSAGE_ID_PREFIX),
            event_type=event_type,
            body=build_webhook_body(event_type, occurred_at=now, data=data, is_test=True),
            due_at=now,
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

    async def resend_delivery(
        self, account_access: AccountAccess, *, delivery_public_id: str
    ) -> AccountWebhookDeliveryResponse:
        """Due now with the same ``webhook-id`` and body; the retry schedule starts over."""
        await self.refuse_unless_webhook_sendable(account_access.account_id)
        delivery = await self.repository.requeue_account_delivery(
            delivery_public_id,
            account_id=account_access.account_id,
            receiver_kind=AccountWebhookReceiverKind.STANDARD_WEBHOOK,
            due_at=datetime.now(UTC),
        )
        if delivery is None:
            raise AccountWebhookDeliveryNotFoundError(f"no webhook delivery {delivery_public_id} on this account")
        log.emit(
            logger,
            event=log_events.AccountWebhookDeliveryQueued(
                account_id=account_access.account_id,
                delivery_id=delivery.id,
                event_type=delivery.event_type,
                cause="resend",
            ),
        )
        return present_delivery(delivery)

    async def queue_claimed_account_events(self, account_events: list[ClaimedAccountEvent]) -> None:
        """Queues each event the webhook sends (``is_webhook_event``, the same rule an API token reads events by) whose
        account set its webhook before it and no 410 disabled it (the endpoint rows locked for share, so a disabling
        waits and then fails these too); ``account.*`` and ``operator.alert`` are not part of the webhook's contract
        (PLAN.md section 8.3). An invoice event also goes to its invoice's ``callback_url``, if it has one."""
        invoice_events = [
            account_event
            for account_event in account_events
            if is_webhook_event(
                invoice_id=account_event.account_event_response.invoice_id,
                event_type=account_event.account_event_response.type,
            )
        ]
        endpoints_by_account_id = {
            endpoint.account_id: endpoint
            for endpoint in await self.repository.list_endpoints_of_accounts(
                sorted({invoice_event.account_id for invoice_event in invoice_events}), lock_for_share=True
            )
        }
        now = datetime.now(UTC)
        for invoice_event in invoice_events:
            endpoint = endpoints_by_account_id.get(invoice_event.account_id)
            if endpoint is None:
                continue
            await self.queue_invoice_callback(invoice_event, now=now)
            if (
                endpoint.disabled_at is not None
                or invoice_event.account_event_response.created_at < endpoint.created_at
            ):
                continue
            delivery = await self.repository.insert_delivery(
                invoice_event.account_id,
                receiver_kind=AccountWebhookReceiverKind.STANDARD_WEBHOOK,
                account_event_id=invoice_event.account_event_id,
                webhook_message_id=invoice_event.account_event_response.id,
                event_type=invoice_event.account_event_response.type,
                body=build_webhook_body(
                    invoice_event.account_event_response.type,
                    occurred_at=invoice_event.account_event_response.created_at,
                    data=invoice_event.account_event_response.data,
                ),
                due_at=now,
            )
            if delivery is not None:
                log.emit(
                    logger,
                    event=log_events.AccountWebhookDeliveryQueued(
                        account_id=invoice_event.account_id,
                        delivery_id=delivery.id,
                        event_type=delivery.event_type,
                        cause="event",
                    ),
                )

    async def queue_invoice_callback(self, invoice_event: ClaimedAccountEvent, *, now: datetime) -> None:
        """Queues the event to its invoice's ``callback_url`` when the invoice has one (read from the event's invoice
        view, so the URL is the one the invoice was made with)."""
        if invoice_event.account_event_response.invoice_id is None:
            return
        invoice_data = invoice_event.account_event_response.data
        callback_url = invoice_data.get("callback_url") if isinstance(invoice_data, dict) else None
        if not isinstance(callback_url, str) or not callback_url:
            return
        delivery = await self.repository.insert_delivery(
            invoice_event.account_id,
            receiver_kind=AccountWebhookReceiverKind.INVOICE_CALLBACK,
            account_event_id=invoice_event.account_event_id,
            webhook_message_id=f"{INVOICE_CALLBACK_MESSAGE_ID_PREFIX}{invoice_event.account_event_response.id}",
            event_type=invoice_event.account_event_response.type,
            body=build_webhook_body(
                invoice_event.account_event_response.type,
                occurred_at=invoice_event.account_event_response.created_at,
                data=invoice_event.account_event_response.data,
            ),
            due_at=now,
            receiver_url=callback_url,
        )
        if delivery is not None:
            log.emit(
                logger,
                event=log_events.AccountWebhookDeliveryQueued(
                    account_id=invoice_event.account_id,
                    delivery_id=delivery.id,
                    event_type=delivery.event_type,
                    cause="invoice_callback",
                ),
            )


async def check_invoice_callback_url(session: AsyncSession, account_id: int, callback_url: str) -> None:  # noqa: PLR0917  # InvoiceCallbackUrlCheck's positional signature
    """The invoices domain's ``InvoiceCallbackUrlCheck``, wired by app/lifespan (this domain sits above invoices)."""
    await AccountWebhookService.on_session(session).check_invoice_callback_url(account_id, callback_url=callback_url)


def read_sent_webhook_id(delivery: AccountWebhookDelivery) -> str:
    """The ``webhook-id`` header: the stored message id, except an invoice callback's, which is sent with the account
    event's public id like the account webhook's delivery of the same event (the stored one only keeps the rows
    unique). A delivery queued before migration 0036 keeps the integer event id it was first sent with."""
    if delivery.receiver_kind == AccountWebhookReceiverKind.INVOICE_CALLBACK.value:
        return delivery.webhook_message_id.removeprefix(INVOICE_CALLBACK_MESSAGE_ID_PREFIX)
    return delivery.webhook_message_id
