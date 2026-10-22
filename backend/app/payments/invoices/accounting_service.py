"""The money rules of PLAN.md sections 4 and 6 as pure functions: crediting a finalized rise in gross received,
the remainder, and who may get what back.

- Each finalized rise in gross received is a credit. A fiat invoice values it at the quote live at the block time of
  the newest final payment in the rise (the finalized head's time for a rise no payment row explains), else at the
  feed price when the watcher sees it final; a crypto invoice counts base units.
- Paid once the shortfall is at most ``accuracy_percent`` of the amount. Whatever comes after the amount due is
  filled is excess; an excess worth at least ``minimum_refund_usd`` makes the invoice overpaid.
- Refund allocation: final payments in chain order fill the amount due first, the rest belongs to its sender, minus
  what was already refunded to that sender. Received money no final payment row explains (a contract's internal
  transfer, or a payment whose attribution lags) is an unknown sender's and comes last; it is never stored as a row,
  so a late attributed row cannot make the rows sum past what was received, and it is never refunded automatically.
"""

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from pydantic import BaseModel, ConfigDict

from app.payments.invoices.enums import InvoiceStatus
from app.shared.amounts import ceil_to_base_units

HUNDRED = Decimal(100)


class AccountingState(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: InvoiceStatus
    received_base_units: int
    credited_fiat_amount: Decimal
    excess_base_units: int


class InvoiceTerms(BaseModel):
    """What the invoice asks: a fiat amount, or a crypto amount in base units."""

    model_config = ConfigDict(frozen=True)

    fiat_amount: Decimal | None
    crypto_amount_base_units: int | None
    accuracy_percent: Decimal
    decimals: int
    quote_decimals: int
    minimum_refund_usd: Decimal


class CreditPrices(BaseModel):
    """Prices a credit is valued at: fiat units per whole asset unit (fiat invoices) and USD per whole unit."""

    model_config = ConfigDict(frozen=True)

    asset_price_in_fiat: Decimal | None
    asset_price_usd: Decimal


class PaymentShare(BaseModel):
    model_config = ConfigDict(frozen=True)

    sender: str | None
    sender_has_code: bool
    amount_base_units: int


class SenderRefundable(BaseModel):
    model_config = ConfigDict(frozen=True)

    sender: str
    sender_has_code: bool
    refundable_base_units: int


class RefundableSplit(BaseModel):
    model_config = ConfigDict(frozen=True)

    senders: list[SenderRefundable]
    unknown_sender_base_units: int


def whole_units(base_units: int, *, decimals: int) -> Decimal:
    return Decimal(base_units).scaleb(-decimals)


def is_paid_or_over(status: InvoiceStatus) -> bool:
    return status.is_settled_by_payment


def apply_credit(
    state: AccountingState, *, gross_received_base_units: int, terms: InvoiceTerms, prices: CreditPrices
) -> AccountingState:
    """The state after crediting the rise from ``state.received_base_units`` to ``gross_received_base_units``."""
    credit_base_units = gross_received_base_units - state.received_base_units
    if credit_base_units <= 0:
        return state
    received = gross_received_base_units
    credited_fiat = state.credited_fiat_amount
    excess = state.excess_base_units
    already_paid = is_paid_or_over(state.status)
    paid = already_paid
    if terms.fiat_amount is not None:
        price_in_fiat = prices.asset_price_in_fiat
        if price_in_fiat is None:
            raise ValueError("a fiat invoice's credit needs a fiat price")
        credited_fiat += whole_units(credit_base_units, decimals=terms.decimals) * price_in_fiat
        if already_paid:
            excess += credit_base_units
        elif credited_fiat >= terms.fiat_amount * (1 - terms.accuracy_percent / HUNDRED):
            paid = True
            over_fiat = credited_fiat - terms.fiat_amount
            if over_fiat > 0:
                over_base_units = int((over_fiat / price_in_fiat).scaleb(terms.decimals).to_integral_value(ROUND_FLOOR))
                excess += min(credit_base_units, over_base_units)
    else:
        due = terms.crypto_amount_base_units or 0
        if already_paid:
            excess += credit_base_units
        else:
            threshold = (Decimal(due) * (1 - terms.accuracy_percent / HUNDRED)).to_integral_value(ROUND_CEILING)
            if received >= threshold:
                paid = True
                excess = max(0, received - due)
    if not paid:
        status = InvoiceStatus.UNDERPAID
    elif whole_units(excess, decimals=terms.decimals) * prices.asset_price_usd >= terms.minimum_refund_usd:
        status = InvoiceStatus.OVERPAID
    else:
        status = InvoiceStatus.PAID
    if state.status == InvoiceStatus.OVERPAID:
        status = InvoiceStatus.OVERPAID
    return AccountingState(
        status=status, received_base_units=received, credited_fiat_amount=credited_fiat, excess_base_units=excess
    )


def remaining_base_units(
    state: AccountingState, *, terms: InvoiceTerms, asset_price_in_fiat: Decimal | None
) -> int | None:
    """What is still to pay, in base units at ``asset_price_in_fiat`` for a fiat invoice; 0 once paid."""
    if is_paid_or_over(state.status):
        return 0
    if terms.fiat_amount is not None:
        if asset_price_in_fiat is None:
            return None
        remaining_fiat = terms.fiat_amount - state.credited_fiat_amount
        if remaining_fiat <= 0:
            return 0
        return ceil_to_base_units(
            remaining_fiat / asset_price_in_fiat, decimals=terms.decimals, quote_decimals=terms.quote_decimals
        )
    return max(0, (terms.crypto_amount_base_units or 0) - state.received_base_units)


def split_refundable(
    final_payments_in_chain_order: Sequence[PaymentShare],
    *,
    received_base_units: int,
    filled_base_units: int,
    refunded_by_sender: dict[str, int],
) -> RefundableSplit:
    """Each sender's own unrefunded part after ``filled_base_units`` filled the amount due (PLAN.md section 6).
    Only ``received_base_units`` is shared out: rows past it (attributed but not yet credited) get nothing."""
    attributed = 0
    shares: list[PaymentShare] = []
    for payment in final_payments_in_chain_order:
        share = min(payment.amount_base_units, max(0, received_base_units - attributed))
        attributed += share
        shares.append(
            PaymentShare(sender=payment.sender, sender_has_code=payment.sender_has_code, amount_base_units=share)
        )
    shares.append(PaymentShare(sender=None, sender_has_code=False, amount_base_units=received_base_units - attributed))
    left_to_fill = filled_base_units
    by_sender: dict[str, int] = {}
    has_code: dict[str, bool] = {}
    unknown = 0
    for payment in shares:
        filling = min(payment.amount_base_units, left_to_fill)
        left_to_fill -= filling
        leftover = payment.amount_base_units - filling
        if leftover <= 0:
            continue
        if payment.sender is None:
            unknown += leftover
            continue
        by_sender[payment.sender] = by_sender.get(payment.sender, 0) + leftover
        has_code[payment.sender] = has_code.get(payment.sender, False) or payment.sender_has_code
    return RefundableSplit(
        senders=[
            SenderRefundable(
                sender=sender,
                sender_has_code=has_code[sender],
                refundable_base_units=max(0, amount - refunded_by_sender.get(sender, 0)),
            )
            for sender, amount in by_sender.items()
        ],
        unknown_sender_base_units=unknown,
    )
