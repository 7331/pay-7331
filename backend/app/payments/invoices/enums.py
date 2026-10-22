"""The invoice statuses of PLAN.md section 4, and the other closed sets of the invoices domain."""

from enum import StrEnum


class InvoiceCreationChannel(StrEnum):
    """Who asked for an invoice, which decides its limits (PLAN.md section 9)."""

    # A browser, signed in or not: the per-client-network open-invoice cap.
    WEB = "web"
    # An account's own server with an API token (2026-10-12): the per-account open-invoice cap instead of the
    # per-network one.
    API_TOKEN = "api_token"  # noqa: S105  # a channel name, not a secret
    # An account's invoice schedule, made by the watcher when a period comes due (payments/invoice_schedules,
    # 2026-10-21): the per-account cap, as an API token's; no client network made it.
    SCHEDULE = "schedule"

    @property
    def is_capped_per_account(self) -> bool:
        """An account's own server or its schedule: counted against the account's open invoices, not a network's."""
        return self in {InvoiceCreationChannel.API_TOKEN, InvoiceCreationChannel.SCHEDULE}


class InvoiceStatus(StrEnum):
    AWAITING_ASSET = "awaiting_asset"
    AWAITING_PAYMENT = "awaiting_payment"
    CONFIRMING = "confirming"
    UNDERPAID = "underpaid"
    PAID = "paid"
    OVERPAID = "overpaid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_settled_by_payment(self) -> bool:
        """Paid in full or over: the deposit is swept."""
        match self:
            case InvoiceStatus.PAID | InvoiceStatus.OVERPAID:
                return True
            case (
                InvoiceStatus.AWAITING_ASSET
                | InvoiceStatus.AWAITING_PAYMENT
                | InvoiceStatus.CONFIRMING
                | InvoiceStatus.UNDERPAID
                | InvoiceStatus.EXPIRED
                | InvoiceStatus.CANCELLED
            ):
                return False


class InvoiceEventType(StrEnum):
    """Invoice and refund event types, kept as account events (PLAN.md section 8.1)."""

    PAYMENT_DETECTED = "invoice.payment_detected"
    # XMR only (PLAN.md section 14): a pool transaction pays the subaddress; not in a block, so nothing changes yet.
    SEEN = "invoice.seen"
    # XMR only: a payment's check on a second node operator disagreed, so the invoice is held at `confirming`, nothing
    # credits, and the operator was told (PLAN.md section 14).
    PAYMENT_HELD = "invoice.payment_held"
    UNDERPAID = "invoice.underpaid"
    PAID = "invoice.paid"
    OVERPAID = "invoice.overpaid"
    EXPIRED = "invoice.expired"
    CANCELLED = "invoice.cancelled"
    SWEPT = "invoice.swept"
    SWEEP_FAILED = "invoice.sweep_failed"
    LATE_PAYMENT = "invoice.late_payment"
    # Final money in a coin the invoice did not ask for, seen while it was live; never counted (INC-001, 2026-10-11).
    STRAY_PAYMENT = "invoice.stray_payment"
    REFUNDED = "invoice.refunded"
    REFUND_WAITING_FOR_FLOAT = "refund.waiting_for_float"
    REFUND_NEEDS_OWNER = "refund.needs_owner"
    # An invoice schedule made this invoice for a period that came due (payments/invoice_schedules, 2026-10-21): the
    # account's server sends its customer the invoice's `url`, since pay-7331 sends no email. Its data is the invoice,
    # whose `invoice_schedule_id` and `invoice_schedule_period_start` name the schedule and the period.
    SCHEDULED_INVOICE_CREATED = "schedule.invoice_created"


class FiatCurrency(StrEnum):
    """The fiat currencies an invoice, a quantity link or a schedule can be priced in."""

    USD = "USD"
    EUR = "EUR"


class InvoiceSweepStatus(StrEnum):
    """A sweep as an invoice shows it: ``final`` once its transfer is in a final block; while under way the status of
    the operation moving it; ``failed`` when that operation failed."""

    FINAL = "final"
    PENDING = "pending"
    WAITING_FOR_FLOAT = "waiting_for_float"
    BROADCAST = "broadcast"
    MINED = "mined"
    FAILED = "failed"


class RefundUnavailableReason(StrEnum):
    """Why ``refund_offer.available`` is false."""

    NOT_OVERPAID = "not_overpaid"
    # The invoice pays a receiver other than the operator: an excess or an underpaid amount is the receiver's to
    # settle; the operator float refunds only what was paid to the operator (2026-10-09).
    RECEIVER_SETTLES = "receiver_settles"
    # Underpaid and before expires_at: top-ups only for now (2026-10-09).
    PAYMENT_WINDOW_OPEN = "payment_window_open"
    BELOW_MINIMUM = "below_minimum"
    # No USD price right now, so the minimum cannot be checked: try again shortly, never "below the minimum"
    # (2026-10-13).
    PRICE_UNAVAILABLE = "price_unavailable"
    UNKNOWN_SENDER = "unknown_sender"
    CONTRACT_SENDER = "contract_sender"
    REFUND_IN_PROGRESS = "refund_in_progress"
    ALREADY_REFUNDED = "already_refunded"


class StrayRefundHandling(StrEnum):
    """What becomes of a stray payment:
    ``automatic`` goes back to its sender from the float once final (its refund shows in ``refunds``);
    ``receiver_settles`` went to someone else's address with the sweep; ``owner_settles`` came from a contract or an
    unknown sender; ``below_minimum`` is too small to send back."""

    AUTOMATIC = "automatic"
    RECEIVER_SETTLES = "receiver_settles"
    OWNER_SETTLES = "owner_settles"
    BELOW_MINIMUM = "below_minimum"


class QrKind(StrEnum):
    """``payment`` encodes the EIP-681 URI; ``address`` only the deposit address, for wallets that read no more."""

    PAYMENT = "payment"
    ADDRESS = "address"


class ChainTransactionState(StrEnum):
    """What the chain's RPC endpoints say of a transaction hash: no endpoint knows it, known but not in a block yet,
    mined and reverted (receipt status 0: nothing moved), or mined and succeeded."""

    UNKNOWN = "unknown"
    NOT_MINED = "not_mined"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class PaymentCheckOutcome(StrEnum):
    """What the payer's "I paid" check found. The deposit reread answers
    ``payment_counted``, ``payment_confirming``, ``wrong_coin_confirming``, ``wrong_coin_recorded`` or
    ``nothing_found``; a transaction hash says what the chain shows of that transaction."""

    # The invoice's coin into this deposit, final and counted.
    PAYMENT_COUNTED = "payment_counted"
    # The invoice's coin into this deposit, not final yet (or final and counted on the watcher's next tick).
    PAYMENT_CONFIRMING = "payment_confirming"
    # The invoice's coin into this deposit after the invoice closed: late money, never counted.
    PAYMENT_AFTER_CLOSE = "payment_after_close"
    # A listed coin the invoice did not ask for, final and recorded as stray money; the refund rules apply.
    WRONG_COIN_RECORDED = "wrong_coin_recorded"
    # A listed coin the invoice did not ask for, not final yet.
    WRONG_COIN_CONFIRMING = "wrong_coin_confirming"
    # A token the chain's list does not have, into this deposit: the seller sorts it out.
    UNLISTED_TOKEN = "unlisted_token"  # noqa: S105  # an outcome code, not a secret
    # The transaction moved nothing into this deposit (never says whether it paid another deposit).
    OTHER_ADDRESS = "other_address"
    TRANSACTION_FAILED = "transaction_failed"
    TRANSACTION_NOT_MINED = "transaction_not_mined"
    # A well-formed hash no endpoint of the chain knows: likely sent on another network.
    TRANSACTION_UNKNOWN = "transaction_unknown"
    # Not 0x and 64 hex digits: a Tron, Solana or Bitcoin id, or a typo.
    HASH_MALFORMED = "hash_malformed"
    NOTHING_FOUND = "nothing_found"


class MoneroCrossCheckState(StrEnum):
    """A Monero payment's check on a second node of another operator (PLAN.md section 14)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    # The second operator's copy disagrees: the invoice is held at `confirming` and the owner is told; nothing credits.
    MISMATCH = "mismatch"
