"""Sending an account's claimed Discord messages (2026-10-13; PLAN.md section 8.4).

Each is a POST of its stored JSON body to Execute Webhook (discord_webhook_urls.py), through the same resolve-and-check
as a webhook (receiver_addresses.py), no redirects, ``ATTEMPT_TIMEOUT_SECONDS``. One account's messages go one after
another, in event order, and stop at the first that is not delivered or that spends the webhook's rate limit: the rest
are held back unsent, due when the account's sends may go again (delivery_service.py), so none overtakes an earlier
one. The whole of an account's sends stops at ``DISCORD_SENDS_TIMEOUT_SECONDS``, well inside the claim's lease, and the
rest are held back the same way. Discord's answers
(https://github.com/discord/discord-api-docs/blob/main/developers/topics/rate-limits.mdx):

- 2xx: delivered (``wait=true`` answers 200 once the message is saved). When ``X-RateLimit-Remaining`` is 0, the account's next messages wait
  until ``X-RateLimit-Reset-After`` seconds from now ("should parse response headers to prevent hitting the limit").
- 429: retried after the body's ``retry_after`` (else the ``Retry-After`` header), outside the retry schedule, and the
  account's next messages wait as long ("rely on the ``Retry-After`` header or ``retry_after`` field"). ``"global":
  true``, or no JSON body, pauses every account's sends (store.py).
- Any 404, or JSON ``code`` 10015 (Unknown Webhook) or 50027 (Invalid Webhook Token, which comes with a 401)
  (https://github.com/discord/discord-api-docs/blob/main/developers/topics/opcodes-and-status-codes.mdx, "JSON Error
  Codes"): the webhook (or the thread it posts to) was deleted or its token is wrong. "If a webhook returns a 404
  status you should not attempt to use it again - repeated attempts to do so will result in a temporary restriction"
  (rate-limits.mdx), so the delivery fails at once and the loop marks the webhook deleted (discord_service.py);
  nothing more is sent to it.
- 400: Discord refused this message's body. Sending the same body again gets the same answer, so it fails at once
  instead of holding the account's later messages for the whole retry schedule. JSON ``code`` 220001 ("Webhooks
  posted to forum channels must have a thread_name or thread_id") is named apart, so /settings can say to paste the
  URL with ``?thread_id=``.
- Anything else (5xx, a 401 or 403 without one of those codes, a timeout): a failure on the retry schedule.

A 401, 403 or 429 not scoped ``shared`` counts toward Discord's invalid request limit per IP (store.py). Clients must
send ``User-Agent: DiscordBot ($url, $versionNumber)`` (reference.mdx, "User Agent"). Nothing logs the URL (it carries
the token) or the body.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import math

import httpx

from app.payments.account_webhooks.discord_webhook_urls import build_discord_execute_webhook_url
from app.payments.account_webhooks.enums import AccountWebhookFailureReason
from app.payments.account_webhooks.models import AccountWebhookDelivery
from app.payments.account_webhooks.receiver_addresses import ReceiverAddressRefusedError, resolve_receiver_address
from app.payments.account_webhooks.store import UNEXPLAINED_RATE_LIMIT_PAUSE_SECONDS, DiscordSendPauseStore
from app.payments.account_webhooks.types import (
    AccountDiscordSendResult,
    AttemptOutcome,
    DiscordDeliveryTarget,
    DiscordMessageAnswer,
)

ATTEMPT_TIMEOUT_SECONDS = 15.0
# An account's sends in one pass stop here: half the claim's two-minute lease, leaving the rest for recording.
DISCORD_SENDS_TIMEOUT_SECONDS = 60.0
DISCORD_USER_AGENT = "DiscordBot (https://pay.7331.org, 1)"
# Unknown Webhook, Invalid Webhook Token (opcodes-and-status-codes.mdx, "JSON Error Codes").
DISCORD_WEBHOOK_GONE_CODES = frozenset({10015, 50027})
# Webhooks posted to forum channels must have a thread_name or thread_id (opcodes-and-status-codes.mdx).
DISCORD_THREAD_REQUIRED_CODE = 220001
TOO_MANY_REQUESTS_STATUS = 429
BAD_REQUEST_STATUS = 400
NOT_FOUND_STATUS = 404
# The answers Discord counts toward its invalid request limit (rate-limits.mdx), but a 429 scoped `shared`.
INVALID_REQUEST_STATUSES = frozenset({401, 403, 429})
SHARED_RATE_LIMIT_SCOPE = "shared"
# Bounds on a wait Discord names: at least a second, at most an hour (a longer one is retried after an hour and is
# told again); a 429 that names none waits the shortest.
MINIMUM_RATE_LIMIT_WAIT_SECONDS = 1.0
MAXIMUM_RATE_LIMIT_WAIT_SECONDS = 3600.0


def read_wait_seconds(text: str | float | None) -> float | None:
    try:
        wait_seconds = float(text) if text is not None else None
    except ValueError:
        return None
    if wait_seconds is None or math.isnan(wait_seconds):
        return None
    return min(max(wait_seconds, MINIMUM_RATE_LIMIT_WAIT_SECONDS), MAXIMUM_RATE_LIMIT_WAIT_SECONDS)


def read_json_object(response: httpx.Response) -> dict[str, object] | None:
    """Discord's JSON answer (an error's ``code`` and ``message``, a 429's ``retry_after`` and ``global``), else None."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def read_retry_after_seconds(response: httpx.Response, *, body: dict[str, object] | None) -> float:
    """A 429's wait: the JSON ``retry_after`` (fractional seconds), else ``Retry-After``, else the minimum."""
    body_retry_after = body.get("retry_after") if body is not None else None
    if isinstance(body_retry_after, int | float) and not isinstance(body_retry_after, bool):
        wait_seconds = read_wait_seconds(body_retry_after)
    else:
        wait_seconds = read_wait_seconds(response.headers.get("Retry-After"))
    return wait_seconds if wait_seconds is not None else MINIMUM_RATE_LIMIT_WAIT_SECONDS


def read_spent_bucket_reset_seconds(response: httpx.Response) -> float | None:
    """Seconds until the bucket refills when this answer spent its last request, else None."""
    if response.headers.get("X-RateLimit-Remaining") != "0":
        return None
    return read_wait_seconds(response.headers.get("X-RateLimit-Reset-After")) or MINIMUM_RATE_LIMIT_WAIT_SECONDS


def read_discord_answer(response: httpx.Response, *, receiver_host: str) -> DiscordMessageAnswer:
    """One answer's outcome, when the account's next message may go, and whether it counts as an invalid request or
    pauses every send (a global or unexplained 429); the caller tells Redis."""
    answered_at = datetime.now(UTC)
    status_code = response.status_code
    if 200 <= status_code < 300:
        reset_seconds = read_spent_bucket_reset_seconds(response)
        return DiscordMessageAnswer(
            outcome=AttemptOutcome(
                delivered=True, response_status=status_code, failure_reason=None, receiver_host=receiver_host
            ),
            rate_limit_hold_until=(
                answered_at + timedelta(seconds=reset_seconds) if reset_seconds is not None else None
            ),
        )
    counts_as_invalid_request = (
        status_code in INVALID_REQUEST_STATUSES and response.headers.get("X-RateLimit-Scope") != SHARED_RATE_LIMIT_SCOPE
    )
    body = read_json_object(response)
    if status_code == TOO_MANY_REQUESTS_STATUS:
        retry_after_seconds = read_retry_after_seconds(response, body=body)
        sending_pause_seconds: float | None = None
        sending_pause_reason: str | None = None
        if body is None:
            sending_pause_seconds = max(retry_after_seconds, UNEXPLAINED_RATE_LIMIT_PAUSE_SECONDS)
            sending_pause_reason = "rate_limit_without_json_body"
        elif body.get("global") is True:
            sending_pause_seconds = retry_after_seconds
            sending_pause_reason = "global_rate_limit"
        retry_at = answered_at + timedelta(seconds=retry_after_seconds)
        return DiscordMessageAnswer(
            outcome=AttemptOutcome(
                delivered=False,
                response_status=status_code,
                failure_reason=AccountWebhookFailureReason.RATE_LIMITED,
                receiver_host=receiver_host,
                retry_at=retry_at,
            ),
            rate_limit_hold_until=retry_at,
            counts_as_invalid_request=counts_as_invalid_request,
            sending_pause_seconds=sending_pause_seconds,
            sending_pause_reason=sending_pause_reason,
        )
    discord_error_code = body.get("code") if body is not None else None
    if status_code == NOT_FOUND_STATUS or discord_error_code in DISCORD_WEBHOOK_GONE_CODES:
        failure_reason = AccountWebhookFailureReason.DISCORD_WEBHOOK_DELETED
    elif status_code == BAD_REQUEST_STATUS and discord_error_code == DISCORD_THREAD_REQUIRED_CODE:
        failure_reason = AccountWebhookFailureReason.DISCORD_THREAD_REQUIRED
    elif status_code == BAD_REQUEST_STATUS:
        failure_reason = AccountWebhookFailureReason.DISCORD_REFUSED_MESSAGE
    else:
        failure_reason = AccountWebhookFailureReason.HTTP_STATUS
    return DiscordMessageAnswer(
        outcome=AttemptOutcome(
            delivered=False, response_status=status_code, failure_reason=failure_reason, receiver_host=receiver_host
        ),
        rate_limit_hold_until=None,
        counts_as_invalid_request=counts_as_invalid_request,
    )


async def send_discord_message(
    delivery: AccountWebhookDelivery,
    *,
    http_client: httpx.AsyncClient,
    target: DiscordDeliveryTarget,
    api_base_url: str,
    allow_private_hosts: bool,
) -> DiscordMessageAnswer:
    """One message."""
    execute_webhook_url = build_discord_execute_webhook_url(
        target.discord_webhook_id,
        discord_webhook_token=target.discord_webhook_token,
        discord_thread_id=target.discord_thread_id,
        api_base_url=api_base_url,
    )
    try:
        checked_address = await resolve_receiver_address(execute_webhook_url, allow_private_hosts=allow_private_hosts)
    except ReceiverAddressRefusedError as refusal:
        outcome = AttemptOutcome(delivered=False, response_status=None, failure_reason=refusal.reason, receiver_host="")
        return DiscordMessageAnswer(outcome=outcome, rate_limit_hold_until=None)
    receiver_host = checked_address.hostname
    try:
        response = await http_client.post(
            checked_address.request_url,
            content=delivery.body.encode(),
            headers={
                "Host": checked_address.host_header,
                "Content-Type": "application/json",
                "User-Agent": DISCORD_USER_AGENT,
            },
            extensions={"sni_hostname": checked_address.hostname} if checked_address.scheme == "https" else {},
            timeout=ATTEMPT_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        failure_reason = AccountWebhookFailureReason.TIMEOUT
    except httpx.HTTPError:
        failure_reason = AccountWebhookFailureReason.CONNECTION_FAILED
    else:
        return read_discord_answer(response, receiver_host=receiver_host)
    outcome = AttemptOutcome(
        delivered=False, response_status=None, failure_reason=failure_reason, receiver_host=receiver_host
    )
    return DiscordMessageAnswer(outcome=outcome, rate_limit_hold_until=None)


async def send_account_discord_messages(
    deliveries: list[AccountWebhookDelivery],
    *,
    http_client: httpx.AsyncClient,
    target: DiscordDeliveryTarget,
    api_base_url: str,
    allow_private_hosts: bool,
    discord_send_pause_store: DiscordSendPauseStore,
) -> AccountDiscordSendResult:
    """One account's claimed messages in order, until one is not delivered, the rate limit is spent, every send is
    paused or ``DISCORD_SENDS_TIMEOUT_SECONDS`` runs out (the message then out counts as a timeout). The last answer's
    invalid request count and pause go to Redis after the timeout's block: every answer that has them ends the sends,
    and the timeout must not cancel them once Discord has answered."""
    attempted_outcomes: list[AttemptOutcome] = []
    rate_limit_hold_until: datetime | None = None
    last_answer: DiscordMessageAnswer | None = None
    message_in_flight = False
    try:
        async with asyncio.timeout(DISCORD_SENDS_TIMEOUT_SECONDS):
            for delivery in deliveries:
                if await discord_send_pause_store.is_discord_sending_paused():
                    break
                message_in_flight = True
                last_answer = await send_discord_message(
                    delivery,
                    http_client=http_client,
                    target=target,
                    api_base_url=api_base_url,
                    allow_private_hosts=allow_private_hosts,
                )
                message_in_flight = False
                attempted_outcomes.append(last_answer.outcome)
                rate_limit_hold_until = last_answer.rate_limit_hold_until
                if not last_answer.outcome.delivered or rate_limit_hold_until is not None:
                    break
    except TimeoutError:
        if message_in_flight:
            attempted_outcomes.append(
                AttemptOutcome(
                    delivered=False,
                    response_status=None,
                    failure_reason=AccountWebhookFailureReason.TIMEOUT,
                    receiver_host="",
                )
            )
    if last_answer is not None:
        await tell_redis_of_discord_answer(last_answer, discord_send_pause_store=discord_send_pause_store)
    return AccountDiscordSendResult(attempted_outcomes=attempted_outcomes, rate_limit_hold_until=rate_limit_hold_until)


async def tell_redis_of_discord_answer(
    answer: DiscordMessageAnswer, *, discord_send_pause_store: DiscordSendPauseStore
) -> None:
    """Counts an invalid request and pauses every send on a global or unexplained 429 (store.py; both fail open)."""
    if answer.counts_as_invalid_request:
        await discord_send_pause_store.count_invalid_discord_request()
    if answer.sending_pause_seconds is not None and answer.sending_pause_reason is not None:
        await discord_send_pause_store.pause_discord_sending(
            pause_seconds=answer.sending_pause_seconds, reason=answer.sending_pause_reason
        )
