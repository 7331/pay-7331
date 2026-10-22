"""What a ``monero_payments`` row means for its invoice (PLAN.md section 14), as pure functions the watcher's credit
path and the presenters share.

- In a block: the sidecar wrote a height and a block hash; it nulls both when the block leaves the chain (a reorg).
- Seen: in the pool, not in a block, and no double spend of it seen. Shown to the payer, never credited.
- Just left the pool: not in a block, no double spend seen, and out of the pool for less than
  ``MONERO_POOL_DEPARTURE_GRACE_SECONDS`` (by ``updated_at``, which the sidecar sets when it marks the row out of the
  pool). A transaction mined is gone from the pool before its block is scanned, and a pool read can land on a node
  that never had it; such a row still counts as seen and as waiting, so an on-time payment never expires on that
  gap. Past the grace it was dropped, as far as anyone can tell.
- Credited: ``credited_at`` set, which the watcher does only for a row in a block whose first check on a second operator
  agreed. Credit comes from block data only, never from the pool.
- Counted at final: credited, ``confirmations_final`` confirmations or more at the confirmed tip (the lower of the
  sidecar's tip and a second operator's; the watcher stores it as the invoice's ``latest_reading_block_number``,
  which the payer pages and "I paid" count from), and both checks on a second operator agreed. Gross received is
  the sum of these.
- Waiting for final: seen or just left the pool, or in a block and not counted at final yet. An invoice with such a
  row is not expired or closed by the clock, so a quantity link's slot stays held too.
- Held: in a block and either check on a second operator disagreed: the invoice stays ``confirming``, nothing credits.
"""

from datetime import datetime, timedelta

from app.payments.chains.types import MoneroChainConfiguration
from app.payments.invoices.enums import MoneroCrossCheckState
from app.payments.invoices.models import Invoice, MoneroPayment

# Ten minutes: five mainnet blocks for the sidecar to scan the block a departed transaction landed in.
MONERO_POOL_DEPARTURE_GRACE_SECONDS = 600


def is_in_block(monero_payment: MoneroPayment) -> bool:
    return monero_payment.block_height is not None and monero_payment.block_hash is not None


def is_seen_in_pool(monero_payment: MoneroPayment) -> bool:
    return not is_in_block(monero_payment) and monero_payment.in_pool and not monero_payment.double_spend_seen


def has_just_left_pool(monero_payment: MoneroPayment, *, now: datetime) -> bool:
    return (
        not is_in_block(monero_payment)
        and not monero_payment.in_pool
        and not monero_payment.double_spend_seen
        and monero_payment.updated_at > now - timedelta(seconds=MONERO_POOL_DEPARTURE_GRACE_SECONDS)
    )


def is_on_its_way(monero_payment: MoneroPayment, *, now: datetime) -> bool:
    """Seen in the pool or just left it: unmined, never credited, and still expected in a block."""
    return is_seen_in_pool(monero_payment) or has_just_left_pool(monero_payment, now=now)


def is_seen_by_payer(monero_payment: MoneroPayment, *, now: datetime) -> bool:
    """What the payer page shows as "Seen": in the pool or just left it, or in a block but not credited yet (its first
    check on a second operator still pending), so the page never falls back to the send screen while the money is on
    its way."""
    return is_on_its_way(monero_payment, now=now) or (
        is_in_block(monero_payment) and monero_payment.credited_at is None and not is_held(monero_payment)
    )


def is_creditable(monero_payment: MoneroPayment) -> bool:
    return is_in_block(monero_payment) and monero_payment.cross_check_state == MoneroCrossCheckState.CONFIRMED


def is_credited(monero_payment: MoneroPayment) -> bool:
    return monero_payment.credited_at is not None and is_in_block(monero_payment)


def is_held(monero_payment: MoneroPayment) -> bool:
    """A check on a second operator disagreed with the row's node: the operator settles it, nothing credits."""
    return is_in_block(monero_payment) and MoneroCrossCheckState.MISMATCH in {
        monero_payment.cross_check_state,
        monero_payment.final_cross_check_state,
    }


def count_confirmations(monero_payment: MoneroPayment, *, tip_height: int | None) -> int:
    """1 in the tip block itself; 0 out of a block or with no tip read yet."""
    if monero_payment.block_height is None or tip_height is None:
        return 0
    return max(tip_height - monero_payment.block_height + 1, 0)


def invoice_confirmations_final(invoice: Invoice, *, chain: MoneroChainConfiguration) -> int:
    """The confirmations this invoice's outputs count final at: what the receiver chose when it was made,
    else the chain's ``chains.toml`` default (invoices of the previous release and open picks)."""
    return invoice.confirmations_final if invoice.confirmations_final is not None else chain.confirmations_final


def is_counted_at_final(monero_payment: MoneroPayment, *, tip_height: int | None, confirmations_final: int) -> bool:
    return (
        is_credited(monero_payment)
        and count_confirmations(monero_payment, tip_height=tip_height) >= confirmations_final
        and monero_payment.cross_check_state == MoneroCrossCheckState.CONFIRMED
        and monero_payment.final_cross_check_state == MoneroCrossCheckState.CONFIRMED
    )


def is_waiting_for_final(
    monero_payment: MoneroPayment, *, tip_height: int | None, confirmations_final: int, now: datetime
) -> bool:
    return is_on_its_way(monero_payment, now=now) or (
        is_in_block(monero_payment)
        and not is_counted_at_final(monero_payment, tip_height=tip_height, confirmations_final=confirmations_final)
    )
