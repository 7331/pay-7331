"""Closed sets of the account event domain."""

from enum import StrEnum

from app.payments.account_events.webhook_event_scope import STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX


# The invoice, refund, schedule and static deposit address types are the values of InvoiceEventType and
# StaticDepositAddressEventType, which sit in domains above this one; account_webhooks/enums.py checks at import that
# both stay covered, since reading a stored type missing here fails.
class AccountEventType(StrEnum):
    """What an account event is about: an invoice (``invoice.*``, ``refund.*``, ``schedule.invoice_created``), a static
    deposit address (``static_address.*``), the account itself (``account.*``) or the operator (``operator.alert``)."""

    INVOICE_PAYMENT_DETECTED = "invoice.payment_detected"
    INVOICE_SEEN = "invoice.seen"
    INVOICE_PAYMENT_HELD = "invoice.payment_held"
    INVOICE_UNDERPAID = "invoice.underpaid"
    INVOICE_PAID = "invoice.paid"
    INVOICE_OVERPAID = "invoice.overpaid"
    INVOICE_EXPIRED = "invoice.expired"
    INVOICE_CANCELLED = "invoice.cancelled"
    INVOICE_SWEPT = "invoice.swept"
    INVOICE_SWEEP_FAILED = "invoice.sweep_failed"
    INVOICE_LATE_PAYMENT = "invoice.late_payment"
    INVOICE_STRAY_PAYMENT = "invoice.stray_payment"
    INVOICE_REFUNDED = "invoice.refunded"
    REFUND_WAITING_FOR_FLOAT = "refund.waiting_for_float"
    REFUND_NEEDS_OWNER = "refund.needs_owner"
    SCHEDULE_INVOICE_CREATED = "schedule.invoice_created"
    STATIC_ADDRESS_PAYMENT_RECEIVED = f"{STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX}payment_received"
    STATIC_ADDRESS_STRAY_PAYMENT = f"{STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX}stray_payment"
    STATIC_ADDRESS_SWEPT = f"{STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX}swept"
    STATIC_ADDRESS_SWEEP_FAILED = f"{STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX}sweep_failed"
    # The account itself changed, with no invoice: data is what changed.
    PAYOUT_ADDRESS_CHANGED = "account.payout_address_changed"
    MONERO_WALLET_SET = "account.monero_wallet_set"
    # A passkey added through an operator-minted enrollment link, the recovery path.
    PASSKEY_ENROLLED_BY_LINK = "account.passkey_enrolled_by_link"
    # A passkey added, or deleted, in /settings behind a fresh passkey check.
    PASSKEY_ADDED = "account.passkey_added"
    PASSKEY_DELETED = "account.passkey_deleted"
    # Something the operator must look at (a failed or stuck sweep, a stale feed, ...), on the operator's own account
    # and with no invoice: {"kind", "message", "problem_key", "chain_key", "invoice_id", "first_seen_at",
    # "is_reminder"}.
    OPERATOR_ALERT = "operator.alert"
