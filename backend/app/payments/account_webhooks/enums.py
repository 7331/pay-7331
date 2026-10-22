"""Closed sets of the account webhook domain."""

from enum import StrEnum

from app.payments.account_events.enums import AccountEventType
from app.payments.invoices.enums import InvoiceEventType
from app.payments.static_deposit_addresses.enums import StaticDepositAddressEventType

# Every invoice and static deposit address event is stored as an account event, and reading one whose type is not an
# AccountEventType fails. Those enums sit in domains above account_events, so the cover is checked here, the first
# domain that sees all three.
UNCOVERED_ACCOUNT_EVENT_TYPES = {*InvoiceEventType, *StaticDepositAddressEventType} - set(AccountEventType)
if UNCOVERED_ACCOUNT_EVENT_TYPES:
    raise RuntimeError(f"AccountEventType lacks {sorted(UNCOVERED_ACCOUNT_EVENT_TYPES)}")


class AccountWebhookReceiverKind(StrEnum):
    """Where a delivery goes (2026-10-13): the account's signed Standard Webhooks URL, its Discord channel webhook, or
    the invoice's own callback URL. One account event is queued at most once per kind."""

    STANDARD_WEBHOOK = "standard_webhook"
    DISCORD = "discord"
    # An invoice's own ``callback_url`` (2026-10-21): the invoice's events, signed like the account's webhook with its
    # secret, beside it. At most 16 characters (the column).
    INVOICE_CALLBACK = "invoice_callback"


class AccountWebhookDeliveryStatus(StrEnum):
    # Due at `next_attempt_at` (or out on an attempt until then).
    PENDING = "pending"
    # A 2xx came back.
    DELIVERED = "delivered"
    # The retry schedule ran out, or the webhook was removed; "Resend" queues it again.
    FAILED = "failed"


class AccountWebhookFailureReason(StrEnum):
    # A non-2xx answer; `last_response_status` has it. A 3xx is one too: redirects are never followed.
    HTTP_STATUS = "http_status"
    TIMEOUT = "timeout"
    CONNECTION_FAILED = "connection_failed"
    UNRESOLVABLE_HOST = "unresolvable_host"
    # The host resolved to a loopback, private, link-local or other non-global address (SSRF guard).
    REFUSED_ADDRESS = "refused_address"
    # The account removed its webhook (or Discord webhook) while this was pending.
    NO_WEBHOOK = "no_webhook"
    # Discord answered 429: retried after its `retry_after`, outside the retry schedule, until the schedule's span
    # from when it was queued has passed (then failed).
    RATE_LIMITED = "rate_limited"
    # Discord answered 404, or JSON code 10015 (Unknown Webhook) or 50027 (Invalid Webhook Token): the webhook (or its
    # thread) was deleted in Discord or its token is wrong. Nothing more is sent to it until the account sets a Discord webhook again.
    DISCORD_WEBHOOK_DELETED = "discord_webhook_deleted"
    # Discord answered 400 to this message's body: never retried, so it can't hold the account's later messages.
    DISCORD_REFUSED_MESSAGE = "discord_refused_message"
    # Discord answered JSON code 220001: the webhook's channel is a forum or media channel, which takes a message only
    # into a thread (`thread_id` in the pasted URL). Never retried, like any other 400.
    DISCORD_THREAD_REQUIRED = "discord_thread_required"
    # The webhook receiver answered 410 Gone: Standard Webhooks says it wants no more, so the account's webhook is
    # disabled and its pending deliveries fail; setting the URL again turns it back on.
    RECEIVER_GONE = "receiver_gone"


class AccountDiscordWebhookStatus(StrEnum):
    ACTIVE = "active"
    # Discord answered 404, Unknown Webhook or Invalid Webhook Token (AccountWebhookFailureReason.
    # DISCORD_WEBHOOK_DELETED): sending stopped.
    DELETED_IN_DISCORD = "deleted_in_discord"


# Each value has a sample in sample_webhook_events.py.
class WebhookTestEventType(StrEnum):
    """What "Send test event" can send: ``webhook.test`` (a message), or a sample of each event an EVM invoice of an
    account or a static deposit address can get. Left out: the Monero-only
    ``invoice.seen`` and ``invoice.payment_held``, and the refund events, which only invoices paying the operator's
    own address get."""

    WEBHOOK_TEST = "webhook.test"
    INVOICE_PAYMENT_DETECTED = InvoiceEventType.PAYMENT_DETECTED.value
    INVOICE_UNDERPAID = InvoiceEventType.UNDERPAID.value
    INVOICE_PAID = InvoiceEventType.PAID.value
    INVOICE_OVERPAID = InvoiceEventType.OVERPAID.value
    INVOICE_EXPIRED = InvoiceEventType.EXPIRED.value
    INVOICE_CANCELLED = InvoiceEventType.CANCELLED.value
    INVOICE_SWEPT = InvoiceEventType.SWEPT.value
    INVOICE_SWEEP_FAILED = InvoiceEventType.SWEEP_FAILED.value
    INVOICE_LATE_PAYMENT = InvoiceEventType.LATE_PAYMENT.value
    INVOICE_STRAY_PAYMENT = InvoiceEventType.STRAY_PAYMENT.value
    SCHEDULE_INVOICE_CREATED = InvoiceEventType.SCHEDULED_INVOICE_CREATED.value
    STATIC_ADDRESS_PAYMENT_RECEIVED = StaticDepositAddressEventType.PAYMENT_RECEIVED.value
    STATIC_ADDRESS_STRAY_PAYMENT = StaticDepositAddressEventType.STRAY_PAYMENT.value
    STATIC_ADDRESS_SWEPT = StaticDepositAddressEventType.SWEPT.value
    STATIC_ADDRESS_SWEEP_FAILED = StaticDepositAddressEventType.SWEEP_FAILED.value
