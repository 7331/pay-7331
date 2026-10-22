"""AccountWebhookDeliveryService: sends queued webhook and Discord messages, at least once each (webhooks 2026-10-12,
Discord 2026-10-13; PLAN.md sections 8.3 and 8.4).

Two loops in the API process, each every ``DELIVERY_INTERVAL_SECONDS`` (``app.lifespan``), so neither waits on the
other: a slow Discord or a hung Redis never delays a signed webhook. A webhook pass (``deliver_account_webhooks``):

1. Claims account events no webhook pass took yet (``AccountEventService.claim_account_events_for_webhooks``) and
   queues the invoice events of accounts with a webhook (``AccountWebhookService``), and those and the operator's
   ``operator.alert`` events of accounts with a Discord webhook (``AccountDiscordWebhookService``), one transaction:
   an event is queued exactly when it is claimed, so none is lost between the two and none is queued twice to the
   same receiver kind. Every process that records an event (the watcher, the API, the CLI) only commits the row;
   this claim, in the API, is the one place that queues it.
2. Claims due webhook deliveries and invoice callbacks (``FOR UPDATE SKIP LOCKED``, so the API's other workers take
   others), each leased for ``DELIVERY_LEASE`` by moving its ``next_attempt_at``, commits, sends them and records the
   outcomes. An invoice callback (2026-10-21) goes to the URL it was queued with, signed with the account webhook's
   current secret, under the account event id as ``webhook-id``; its 410 fails it alone and leaves the account's
   webhook on.

A Discord pass (``deliver_account_discord_messages``) does nothing while every Discord send is paused (store.py);
else it claims due Discord messages per account: only accounts whose sends are neither held (``next_discord_send_at``)
nor leased to another pass, the least recently sent first and at most ``DISCORD_MESSAGES_PER_ACCOUNT_PER_CLAIM`` of
each, and leases those accounts too (``discord_send_lease_ends_at``), so one pass at a time sends an account's
messages. Its record transaction locks the accounts' webhook rows first, in ``account_id`` order, then their messages:
the order queuing, removal and replacing take too, so none deadlocks with another.

Every outcome is recorded only while the delivery (and, for Discord, the account) still carries the lease its claim set
(repository.py): a pass recording after its lease ran out, or after the webhook was removed, changes nothing. A crash
after sending and before recording leaves the lease to run out and the message goes again: at least once, so receivers
dedupe on ``webhook-id`` (a Discord channel may show a message twice after such a crash).

Discord messages are sent by discord_delivery.py, one account's in order, stopping at the first not delivered. Their
outcomes set when the account's next ones may go: a spent rate limit's reset, a 429's ``retry_after`` or the failed
message's retry, else at once; the messages held back unsent are due then too, so none overtakes an earlier one. A
Discord 429 is retried outside the schedule until ``RETRY_SCHEDULE_SPAN`` from when the message was queued, then
failed. A 404, Unknown Webhook or Invalid Webhook Token marks the account's Discord webhook deleted and fails its
pending messages, unless the account set another since: then its messages are due again at once, to the new one,
without the old one's hold. The rest of this docstring is about webhook deliveries.

Each attempt (Standard Webhooks, https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md):
a POST of the delivery's stored body with ``webhook-id`` (stable across attempts), ``webhook-timestamp`` (the attempt's
unix seconds) and ``webhook-signature: v1,<base64 HMAC-SHA256 of "{id}.{timestamp}.{body}" under the secret's decoded
bytes>``, signed with the spec's own Python library (``standardwebhooks``,
https://pypi.org/project/standardwebhooks/). Any 2xx is delivered; anything else, a 3xx included (redirects are never
followed, as the spec says), a timeout (``ATTEMPT_TIMEOUT_SECONDS``, inside the spec's 15-30 s) or a refused or
unreachable address is a failure, retried after ``RETRY_DELAYS`` (the spec's example schedule: 5 s, 5 min, 30 min,
2 h, 5 h, 10 h, 14 h, 20 h, 24 h, about 3 days in all) plus up to ``RETRY_JITTER_FRACTION`` of jitter, then failed.
A failure answer's ``Retry-After`` (delay-seconds or an HTTP-date, RFC 9110 section 10.2.3,
https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3; the spec: "should be taken into consideration when scheduling
the next attempt", e.g. on a 429 or 503) puts the next attempt no earlier than it, at most
``MAXIMUM_RETRY_AFTER`` away; the attempt still counts on the schedule, so the attempts stay ten and the whole span
grows by at most nine of those. A 410 Gone ("Sender should disable the webhook endpoint, and stop sending it messages")
fails the delivery at once and disables the account's webhook (service.py) until the URL is set again.
The host is resolved and checked before every attempt (receiver_addresses.py). Nothing logs the URL's path or query,
the body or the secret.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import random

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from standardwebhooks.webhooks import Webhook

from app.payments.account_events.service import AccountEventService
from app.payments.account_webhooks.configuration import read_e2e_discord_api_base_url
from app.payments.account_webhooks.discord_delivery import send_account_discord_messages
from app.payments.account_webhooks.discord_service import AccountDiscordWebhookService
from app.payments.account_webhooks.discord_webhook_urls import DISCORD_API_BASE_URL
from app.payments.account_webhooks.enums import (
    AccountWebhookDeliveryStatus,
    AccountWebhookFailureReason,
    AccountWebhookReceiverKind,
)
from app.payments.account_webhooks.log_events import (
    AccountWebhookDeliveryAttempted,
    AccountWebhookDeliveryNotAttempted,
)
from app.payments.account_webhooks.models import AccountDiscordWebhook, AccountWebhookDelivery
from app.payments.account_webhooks.receiver_addresses import ReceiverAddressRefusedError, resolve_receiver_address
from app.payments.account_webhooks.repository import AccountWebhookRepository
from app.payments.account_webhooks.secret_encryption import (
    derive_discord_webhook_token_encryption_key,
    open_webhook_secret,
)
from app.payments.account_webhooks.service import AccountWebhookService, read_sent_webhook_id
from app.payments.account_webhooks.store import DiscordSendPauseStore
from app.payments.account_webhooks.types import (
    AccountDiscordSendResult,
    AttemptOutcome,
    DiscordDeliveryTarget,
    StandardWebhookDeliveryTarget,
)
from app.payments.invoices.configuration import read_invoice_settings
from app.shared import logging as log
from app.shared.sealed_bytes import SealedBytesUnreadableError

logger = log.get_logger(__name__)

DELIVERY_INTERVAL_SECONDS = 2.0
EVENTS_PER_CLAIM = 200
DELIVERIES_PER_PASS = 20
# So one account's backlog cannot take a whole Discord pass from the others.
DISCORD_MESSAGES_PER_ACCOUNT_PER_CLAIM = 5
ATTEMPT_TIMEOUT_SECONDS = 15.0
# A whole webhook attempt (DNS 5 s, then 15 s per httpx phase, which a receiver trickling bytes could renew) stops
# here, as an account's Discord sends stop at discord_delivery.DISCORD_SENDS_TIMEOUT_SECONDS (60 s).
ATTEMPT_DEADLINE_SECONDS = 60.0
# Twice the longest a pass keeps a delivery out (60 s), so a lease never ends while its attempt is still out or before
# its outcome is recorded.
DELIVERY_LEASE = timedelta(minutes=2)
RETRY_DELAYS = (
    timedelta(seconds=5),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=5),
    timedelta(hours=10),
    timedelta(hours=14),
    timedelta(hours=20),
    timedelta(hours=24),
)
RETRY_JITTER_FRACTION = 0.1
# About 3 days: a Discord message still answered 429 this long after it was queued fails.
RETRY_SCHEDULE_SPAN = sum(RETRY_DELAYS, timedelta())
USER_AGENT = "pay-7331-webhooks/1"
GONE_STATUS = 410
# The longest a receiver's Retry-After holds one delivery: a far date is honoured for an hour, then asked again.
MAXIMUM_RETRY_AFTER = timedelta(hours=1)

type SessionFactory = Callable[[], AsyncSession]


# Failures that end a delivery at once rather than on the schedule.
FINAL_FAILURE_REASONS = frozenset(
    {
        AccountWebhookFailureReason.NO_WEBHOOK,
        AccountWebhookFailureReason.DISCORD_WEBHOOK_DELETED,
        AccountWebhookFailureReason.DISCORD_REFUSED_MESSAGE,
        AccountWebhookFailureReason.DISCORD_THREAD_REQUIRED,
        AccountWebhookFailureReason.RECEIVER_GONE,
    }
)
NO_TARGET_OUTCOME = AttemptOutcome(
    delivered=False, response_status=None, failure_reason=AccountWebhookFailureReason.NO_WEBHOOK, receiver_host=""
)


def schedule_next_attempt(scheduled_failure_count: int, *, failed_at: datetime) -> datetime | None:
    """When to try again after the ``scheduled_failure_count``-th failure (1-based); None once the schedule is out."""
    if scheduled_failure_count > len(RETRY_DELAYS):
        return None
    delay = RETRY_DELAYS[scheduled_failure_count - 1]
    jitter = delay * random.uniform(0, RETRY_JITTER_FRACTION)  # noqa: S311  # spreads retries, not a secret
    return failed_at + delay + jitter


def read_retry_after_until(retry_after_text: str | None, *, answered_at: datetime) -> datetime | None:
    """When a ``Retry-After`` (delay-seconds or an HTTP-date) lets the next attempt go, at most
    ``MAXIMUM_RETRY_AFTER`` after ``answered_at``; None when absent, unreadable or already past."""
    if not retry_after_text:
        return None
    retry_after_text = retry_after_text.strip()
    if retry_after_text.isdecimal():
        wait = timedelta(seconds=min(int(retry_after_text), MAXIMUM_RETRY_AFTER.total_seconds()))
    else:
        try:
            retry_after_moment = parsedate_to_datetime(retry_after_text)
        except (TypeError, ValueError):
            return None
        if retry_after_moment.tzinfo is None:
            retry_after_moment = retry_after_moment.replace(tzinfo=UTC)
        wait = retry_after_moment - answered_at
    if wait <= timedelta():
        return None
    return answered_at + min(wait, MAXIMUM_RETRY_AFTER)


class AccountWebhookDeliveryService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        webhook_http_client: httpx.AsyncClient,
        discord_http_client: httpx.AsyncClient,
        redis: Redis,
        encryption_key: bytes,
        allow_private_hosts: bool,
    ) -> None:
        self.session_factory = session_factory
        self.discord_send_pause_store = DiscordSendPauseStore(client=redis)
        # No kept-alive connections for webhooks (lifespan/composition.py): a pooled connection is reused by IP alone.
        self.webhook_http_client = webhook_http_client
        self.discord_http_client = discord_http_client
        self.encryption_key = encryption_key
        self.allow_private_hosts = allow_private_hosts
        self.discord_token_encryption_key = derive_discord_webhook_token_encryption_key(
            read_invoice_settings().invoice_salt_seed
        )
        # The fork e2e's stand-in (local_fork only, configuration.py), a container on the stack's network; else Discord.
        e2e_discord_api_base_url = read_e2e_discord_api_base_url()
        self.discord_api_base_url = e2e_discord_api_base_url or DISCORD_API_BASE_URL
        self.allow_private_discord_hosts = bool(e2e_discord_api_base_url)

    async def deliver_account_webhooks(self) -> bool:
        """One webhook pass. False when it took a full batch, so the loop runs the next at once."""
        async with self.session_factory() as session, session.begin():
            account_event_service = AccountEventService.on_session(session)
            claimed_event_count, account_events = await account_event_service.claim_account_events_for_webhooks(
                limit=EVENTS_PER_CLAIM
            )
            if account_events:
                await AccountWebhookService.on_session(session).queue_claimed_account_events(account_events)
                await AccountDiscordWebhookService.on_session(session).queue_claimed_account_events(account_events)
        async with self.session_factory() as session, session.begin():
            repository = AccountWebhookRepository(session=session)
            webhook_deliveries = await repository.claim_due_webhook_deliveries(
                limit=DELIVERIES_PER_PASS, lease=DELIVERY_LEASE
            )
            webhook_targets = await self.read_delivery_targets(repository, deliveries=webhook_deliveries)
        await self.deliver_claimed_webhook_deliveries(webhook_deliveries, targets=webhook_targets)
        return claimed_event_count < EVENTS_PER_CLAIM and len(webhook_deliveries) < DELIVERIES_PER_PASS

    async def deliver_account_discord_messages(self) -> bool:
        """One Discord pass. False when it took a full batch, so the loop runs the next at once."""
        if await self.discord_send_pause_store.is_discord_sending_paused():
            return True
        async with self.session_factory() as session, session.begin():
            repository = AccountWebhookRepository(session=session)
            discord_deliveries, lease_ends_at_by_account_id = await repository.claim_due_discord_deliveries(
                limit=DELIVERIES_PER_PASS,
                messages_per_account=DISCORD_MESSAGES_PER_ACCOUNT_PER_CLAIM,
                lease=DELIVERY_LEASE,
            )
            discord_deliveries_by_account_id: dict[int, list[AccountWebhookDelivery]] = defaultdict(list)
            for delivery in discord_deliveries:
                discord_deliveries_by_account_id[delivery.account_id].append(delivery)
            discord_targets = await self.read_discord_delivery_targets(
                repository, account_ids=sorted(discord_deliveries_by_account_id)
            )
        await self.deliver_claimed_discord_messages(
            discord_deliveries_by_account_id,
            targets=discord_targets,
            lease_ends_at_by_account_id=lease_ends_at_by_account_id,
        )
        return len(discord_deliveries) < DELIVERIES_PER_PASS

    async def deliver_claimed_webhook_deliveries(
        self, deliveries: list[AccountWebhookDelivery], *, targets: dict[int, StandardWebhookDeliveryTarget]
    ) -> None:
        if not deliveries:
            return
        # A TaskGroup, not gather: an attempt that raises cancels the others rather than leaving them running unwatched.
        async with asyncio.TaskGroup() as attempt_task_group:
            attempt_tasks = [
                attempt_task_group.create_task(self.attempt_delivery(delivery, target=targets.get(delivery.id)))
                for delivery in deliveries
            ]
        outcomes = [attempt_task.result() for attempt_task in attempt_tasks]
        now = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            repository = AccountWebhookRepository(session=session)
            for delivery, outcome in zip(deliveries, outcomes, strict=True):
                status, _next_attempt_at = await record_attempt(
                    delivery, repository=repository, outcome=outcome, now=now
                )
                emit_delivery_attempted(delivery, outcome=outcome, status=status)
        # A 410 disables the webhook, each account in its own transaction after the outcomes commit: it locks the
        # endpoint and then every pending delivery of the account, which another pass may be recording.
        # An invoice callback's 410 fails that delivery alone (FINAL_FAILURE_REASONS): its URL is not the webhook's.
        gone_urls_by_account_id = {
            delivery.account_id: target.url
            for delivery, outcome in zip(deliveries, outcomes, strict=True)
            if outcome.failure_reason is AccountWebhookFailureReason.RECEIVER_GONE
            and delivery.receiver_kind == AccountWebhookReceiverKind.STANDARD_WEBHOOK.value
            and (target := targets.get(delivery.id)) is not None
        }
        for account_id, gone_url in sorted(gone_urls_by_account_id.items()):
            async with self.session_factory() as session, session.begin():
                await AccountWebhookService.on_session(session).disable_after_receiver_gone(
                    account_id, url=gone_url, disabled_at=now
                )

    async def deliver_claimed_discord_messages(
        self,
        deliveries_by_account_id: dict[int, list[AccountWebhookDelivery]],
        *,
        targets: dict[int, DiscordDeliveryTarget],
        lease_ends_at_by_account_id: dict[int, datetime],
    ) -> None:
        if not deliveries_by_account_id:
            return
        account_ids = sorted(deliveries_by_account_id)
        async with asyncio.TaskGroup() as send_task_group:
            send_tasks = [
                send_task_group.create_task(
                    self.send_discord_messages_of_account(
                        deliveries_by_account_id[account_id], target=targets.get(account_id)
                    )
                )
                for account_id in account_ids
            ]
        send_results = [send_task.result() for send_task in send_tasks]
        now = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            repository = AccountWebhookRepository(session=session)
            discord_webhook_service = AccountDiscordWebhookService.on_session(session)
            # Webhook rows first, in account_id order, then the messages (the order every writer takes).
            current_discord_webhooks_by_account_id = {
                discord_webhook.account_id: discord_webhook
                for discord_webhook in await repository.lock_discord_webhooks_for_recording(account_ids)
            }
            for account_id, send_result in zip(account_ids, send_results, strict=True):
                await record_account_discord_send_result(
                    deliveries_by_account_id[account_id],
                    send_result=send_result,
                    target=targets.get(account_id),
                    current_discord_webhook=current_discord_webhooks_by_account_id.get(account_id),
                    account_lease_ends_at=lease_ends_at_by_account_id.get(account_id),
                    repository=repository,
                    discord_webhook_service=discord_webhook_service,
                    now=now,
                )

    async def send_discord_messages_of_account(
        self, deliveries: list[AccountWebhookDelivery], *, target: DiscordDeliveryTarget | None
    ) -> AccountDiscordSendResult:
        if target is None:
            return AccountDiscordSendResult(
                attempted_outcomes=[NO_TARGET_OUTCOME for _delivery in deliveries], rate_limit_hold_until=None
            )
        return await send_account_discord_messages(
            deliveries,
            http_client=self.discord_http_client,
            target=target,
            api_base_url=self.discord_api_base_url,
            allow_private_hosts=self.allow_private_discord_hosts,
            discord_send_pause_store=self.discord_send_pause_store,
        )

    async def read_delivery_targets(
        self, repository: AccountWebhookRepository, *, deliveries: list[AccountWebhookDelivery]
    ) -> dict[int, StandardWebhookDeliveryTarget]:
        """Each delivery's URL and its account's opened secret, by delivery id: a rotated secret or a changed URL
        applies to the next attempt. A webhook delivery goes to the account's current URL, an invoice callback to its
        own. An account whose secret does not open or that has no webhook gets no target, nor does a webhook delivery
        of an account whose webhook a 410 disabled: those deliveries fail as unsendable."""
        if not deliveries:
            return {}
        secrets_by_account_id: dict[int, str] = {}
        webhook_urls_by_account_id: dict[int, str] = {}
        for endpoint in await repository.list_endpoints_of_accounts(
            sorted({delivery.account_id for delivery in deliveries})
        ):
            try:
                secret = open_webhook_secret(
                    endpoint.encrypted_secret, account_id=endpoint.account_id, encryption_key=self.encryption_key
                )
            except SealedBytesUnreadableError:
                log.emit(logger, event=AccountWebhookDeliveryNotAttempted(account_id=endpoint.account_id))
                continue
            secrets_by_account_id[endpoint.account_id] = secret.get_secret_value()
            if endpoint.disabled_at is None:
                webhook_urls_by_account_id[endpoint.account_id] = endpoint.url
        targets: dict[int, StandardWebhookDeliveryTarget] = {}
        for delivery in deliveries:
            secret_text = secrets_by_account_id.get(delivery.account_id)
            url = (
                delivery.receiver_url
                if delivery.receiver_kind == AccountWebhookReceiverKind.INVOICE_CALLBACK.value
                else webhook_urls_by_account_id.get(delivery.account_id)
            )
            if secret_text is not None and url:
                targets[delivery.id] = StandardWebhookDeliveryTarget(url=url, secret=secret_text)
        return targets

    async def read_discord_delivery_targets(
        self, repository: AccountWebhookRepository, *, account_ids: list[int]
    ) -> dict[int, DiscordDeliveryTarget]:
        """Each account's current Discord webhook, its token opened; none for one deleted in Discord or whose token
        does not open, so its messages fail as unsendable."""
        if not account_ids:
            return {}
        targets: dict[int, DiscordDeliveryTarget] = {}
        for discord_webhook in await repository.list_discord_webhooks_of_accounts(account_ids):
            if discord_webhook.deleted_in_discord_at is not None:
                continue
            try:
                discord_webhook_token = open_webhook_secret(
                    discord_webhook.encrypted_discord_webhook_token,
                    account_id=discord_webhook.account_id,
                    encryption_key=self.discord_token_encryption_key,
                )
            except SealedBytesUnreadableError:
                log.emit(logger, event=AccountWebhookDeliveryNotAttempted(account_id=discord_webhook.account_id))
                continue
            targets[discord_webhook.account_id] = DiscordDeliveryTarget(
                discord_webhook_id=discord_webhook.discord_webhook_id,
                discord_webhook_token=discord_webhook_token,
                discord_thread_id=discord_webhook.discord_thread_id,
            )
        return targets

    async def attempt_delivery(
        self, delivery: AccountWebhookDelivery, *, target: StandardWebhookDeliveryTarget | None
    ) -> AttemptOutcome:
        if target is None:
            return NO_TARGET_OUTCOME
        try:
            async with asyncio.timeout(ATTEMPT_DEADLINE_SECONDS):
                return await self.post_webhook_delivery(delivery, target=target)
        except TimeoutError:
            return AttemptOutcome(
                delivered=False,
                response_status=None,
                failure_reason=AccountWebhookFailureReason.TIMEOUT,
                receiver_host="",
            )

    async def post_webhook_delivery(
        self, delivery: AccountWebhookDelivery, *, target: StandardWebhookDeliveryTarget
    ) -> AttemptOutcome:
        try:
            checked_address = await resolve_receiver_address(target.url, allow_private_hosts=self.allow_private_hosts)
        except ReceiverAddressRefusedError as refusal:
            return AttemptOutcome(
                delivered=False, response_status=None, failure_reason=refusal.reason, receiver_host=""
            )
        attempted_at = datetime.now(UTC)
        sent_webhook_id = read_sent_webhook_id(delivery)
        signature = Webhook(target.secret).sign(msg_id=sent_webhook_id, timestamp=attempted_at, data=delivery.body)
        headers = {
            "Host": checked_address.host_header,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "webhook-id": sent_webhook_id,
            "webhook-timestamp": str(int(attempted_at.timestamp())),
            "webhook-signature": signature,
        }
        extensions = {"sni_hostname": checked_address.hostname} if checked_address.scheme == "https" else {}
        receiver_host = checked_address.hostname
        try:
            async with self.webhook_http_client.stream(
                "POST",
                checked_address.request_url,
                content=delivery.body.encode(),
                headers=headers,
                extensions=extensions,
                timeout=ATTEMPT_TIMEOUT_SECONDS,
            ) as response:
                response_status = response.status_code
                retry_after_text = response.headers.get("Retry-After")
        except httpx.TimeoutException:
            return AttemptOutcome(
                delivered=False,
                response_status=None,
                failure_reason=AccountWebhookFailureReason.TIMEOUT,
                receiver_host=receiver_host,
            )
        except httpx.HTTPError:
            return AttemptOutcome(
                delivered=False,
                response_status=None,
                failure_reason=AccountWebhookFailureReason.CONNECTION_FAILED,
                receiver_host=receiver_host,
            )
        if 200 <= response_status < 300:
            return AttemptOutcome(
                delivered=True, response_status=response_status, failure_reason=None, receiver_host=receiver_host
            )
        return AttemptOutcome(
            delivered=False,
            response_status=response_status,
            failure_reason=(
                AccountWebhookFailureReason.RECEIVER_GONE
                if response_status == GONE_STATUS
                else AccountWebhookFailureReason.HTTP_STATUS
            ),
            receiver_host=receiver_host,
            retry_not_before=read_retry_after_until(retry_after_text, answered_at=datetime.now(UTC)),
        )


def emit_delivery_attempted(
    delivery: AccountWebhookDelivery, *, outcome: AttemptOutcome, status: AccountWebhookDeliveryStatus
) -> None:
    log.emit(
        logger,
        event=AccountWebhookDeliveryAttempted(
            account_id=delivery.account_id,
            delivery_id=delivery.id,
            receiver_kind=delivery.receiver_kind,
            event_type=delivery.event_type,
            attempt_count=delivery.attempt_count,
            receiver_host=outcome.receiver_host,
            delivered=outcome.delivered,
            response_status=outcome.response_status,
            failure_reason=outcome.failure_reason.value if outcome.failure_reason else None,
            status=status.value,
        ),
    )


async def record_account_discord_send_result(
    deliveries: list[AccountWebhookDelivery],
    *,
    send_result: AccountDiscordSendResult,
    target: DiscordDeliveryTarget | None,
    current_discord_webhook: AccountDiscordWebhook | None,
    account_lease_ends_at: datetime | None,
    repository: AccountWebhookRepository,
    discord_webhook_service: AccountDiscordWebhookService,
    now: datetime,
) -> None:
    """Writes one account's sent messages, releases the ones held back unsent and ends the account's lease: its next
    messages may go once the rate limit allows and the failed one (if any) is due again, so none overtakes it. When the
    account set another webhook (or thread) while the messages were out, the old one's hold does not apply: they are
    all due now, to the new one."""
    account_id = deliveries[0].account_id
    discord_webhook_replaced = (
        target is not None
        and current_discord_webhook is not None
        and (current_discord_webhook.discord_webhook_id, current_discord_webhook.discord_thread_id)
        != (target.discord_webhook_id, target.discord_thread_id)
    )
    account_next_send_at = send_result.rate_limit_hold_until or now
    for delivery, outcome in zip(deliveries, send_result.attempted_outcomes, strict=False):
        lease_ends_at = read_lease_end(delivery)
        discord_webhook_deleted = outcome.failure_reason is AccountWebhookFailureReason.DISCORD_WEBHOOK_DELETED
        if discord_webhook_deleted and discord_webhook_replaced:
            await repository.release_attempted_delivery(
                delivery.id,
                lease_ends_at=lease_ends_at,
                next_attempt_at=now,
                response_status=outcome.response_status or 0,
            )
            emit_delivery_attempted(delivery, outcome=outcome, status=AccountWebhookDeliveryStatus.PENDING)
            continue
        status, next_attempt_at = await record_attempt(delivery, repository=repository, outcome=outcome, now=now)
        if discord_webhook_deleted and target is not None:
            await discord_webhook_service.mark_deleted_in_discord(
                account_id,
                discord_webhook_id=target.discord_webhook_id,
                discord_thread_id=target.discord_thread_id,
                response_status=outcome.response_status or 0,
                deleted_at=now,
            )
        if status is AccountWebhookDeliveryStatus.PENDING and next_attempt_at is not None:
            account_next_send_at = max(account_next_send_at, next_attempt_at)
        emit_delivery_attempted(delivery, outcome=outcome, status=status)
    for delivery in deliveries[len(send_result.attempted_outcomes) :]:
        await repository.release_unattempted_delivery(
            delivery.id, lease_ends_at=read_lease_end(delivery), next_attempt_at=account_next_send_at
        )
    if account_lease_ends_at is not None:
        await repository.end_account_discord_send_lease(
            account_id,
            lease_ends_at=account_lease_ends_at,
            next_discord_send_at=now if discord_webhook_replaced else account_next_send_at,
        )
    if discord_webhook_replaced:
        await repository.make_held_discord_deliveries_due_now(account_id)


def read_lease_end(delivery: AccountWebhookDelivery) -> datetime:
    """The lease end the claim set on a delivery: its outcome is recorded only while the row still carries it."""
    if delivery.next_attempt_at is None:
        raise RuntimeError("a claimed delivery has no lease end")
    return delivery.next_attempt_at


async def record_attempt(
    delivery: AccountWebhookDelivery, *, repository: AccountWebhookRepository, outcome: AttemptOutcome, now: datetime
) -> tuple[AccountWebhookDeliveryStatus, datetime | None]:
    """Writes one sent delivery's outcome and answers its status now and, while pending, when it is due again (as
    sent: nothing is written once the delivery no longer carries this pass's lease)."""
    lease_ends_at = read_lease_end(delivery)
    if outcome.delivered and outcome.response_status is not None:
        await repository.record_delivery_success(
            delivery.id, lease_ends_at=lease_ends_at, response_status=outcome.response_status, delivered_at=now
        )
        return AccountWebhookDeliveryStatus.DELIVERED, None
    if (
        outcome.retry_at is not None
        and outcome.response_status is not None
        and now - delivery.queued_at < RETRY_SCHEDULE_SPAN
    ):
        await repository.record_delivery_rate_limited(
            delivery.id,
            lease_ends_at=lease_ends_at,
            response_status=outcome.response_status,
            next_attempt_at=outcome.retry_at,
        )
        return AccountWebhookDeliveryStatus.PENDING, outcome.retry_at
    failure_reason = outcome.failure_reason or AccountWebhookFailureReason.CONNECTION_FAILED
    next_attempt_at = (
        None
        if failure_reason in FINAL_FAILURE_REASONS or outcome.retry_at is not None
        else schedule_next_attempt(delivery.scheduled_failure_count + 1, failed_at=now)
    )
    if next_attempt_at is not None and outcome.retry_not_before is not None:
        next_attempt_at = max(next_attempt_at, outcome.retry_not_before)
    await repository.record_delivery_failure(
        delivery.id,
        lease_ends_at=lease_ends_at,
        response_status=outcome.response_status,
        failure_reason=failure_reason,
        next_attempt_at=next_attempt_at,
    )
    status = AccountWebhookDeliveryStatus.PENDING if next_attempt_at else AccountWebhookDeliveryStatus.FAILED
    return status, next_attempt_at
