"""Invoice rows to the Invoice resource (PLAN.md section 7.1); one function feeds the API and the account events.
Also the account's invoice list cursor and the invoice QR code."""

import base64
import binascii
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
import io

from pydantic import BaseModel, ConfigDict
import segno

from app.payments.accounts.types import SellerSupportContact
from app.payments.chains.service import present_asset, present_chain
from app.payments.chains.types import (
    AssetConfiguration,
    ChainConfiguration,
    MoneroChainConfiguration,
)
from app.payments.invoices.accounting_service import (
    AccountingState,
    InvoiceTerms,
    PaymentShare,
    RefundableSplit,
    remaining_base_units,
    split_refundable,
    whole_units,
)
from app.payments.invoices.enums import (
    InvoiceStatus,
    InvoiceSweepStatus,
    RefundUnavailableReason,
    StrayRefundHandling,
)
from app.payments.invoices.models import (
    DepositSweepEvent,
    Invoice,
    InvoicePayment,
    InvoiceStrayPayment,
    MoneroPayment,
    offered_assets_by_chain_of,
    require_deposit_address,
)
from app.payments.invoices.monero_payment_rules import (
    count_confirmations,
    invoice_confirmations_final,
    is_counted_at_final,
    is_credited,
    is_seen_by_payer,
)
from app.payments.invoices.payment_options import present_payment_options
from app.payments.invoices.payment_uris import build_payment_uri
from app.payments.invoices.schemas import (
    InvoiceAccountResponse,
    InvoiceFiat,
    InvoiceMoneroDetails,
    InvoicePublicPayment,
    InvoicePublicResponse,
    InvoicePublicStrayPayment,
    InvoiceQuote,
    InvoiceRefund,
    InvoiceRefundOffer,
    InvoiceSellerSupportContact,
    InvoiceSweep,
    PaymentOption,
    RefundOfferSender,
)
from app.payments.price_quotes.types import ChainPrices, QuoteValue
from app.payments.settlements.enums import OperationKind, OperationStatus
from app.payments.settlements.types import OperationValue
from app.shared.amounts import format_base_units
from app.shared.cursors import decode_url_safe_base64_cursor
from app.shared.errors import InvalidInputError

CENT = Decimal("0.01")


class InvoiceSnapshot(BaseModel):
    """Everything the resource is built from, read in one go."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    invoice: Invoice
    chain: ChainConfiguration
    payments: list[InvoicePayment]
    # Money in assets the invoice did not ask for (INC-001); never counted.
    stray_payments: list[InvoiceStrayPayment]
    sweep_events: list[DepositSweepEvent]
    operations: list[OperationValue]
    latest_quote: QuoteValue | None
    # None when no feed answered.
    prices: ChainPrices | None
    # While the invoice awaits its pick: every offered chain and, of those, the ones whose feeds answered (PLAN.md
    # section 15); empty otherwise.
    offered_chains: dict[str, ChainConfiguration]
    option_prices_by_chain: dict[str, ChainPrices]
    minimum_refund_usd: Decimal
    public_base_url: str
    # True only on the operator accounts' own invoices paying the operator's own address (``pays_the_operator``): the
    # float refunds only money that went to the operator.
    float_refunds_allowed: bool
    # The invoice's account's support contact; None signed out or unset.
    seller_support_contact: SellerSupportContact | None
    # A Monero invoice's outputs as the scanner sidecar wrote them (PLAN.md section 14); empty on every EVM invoice.
    monero_payments: list[MoneroPayment]

    @property
    def asset(self) -> AssetConfiguration | None:
        symbol = self.invoice.asset_symbol
        return self.chain.find_asset(symbol) if symbol is not None else None


def accounting_state_of(invoice: Invoice) -> AccountingState:
    return AccountingState(
        status=invoice.status,
        received_base_units=invoice.received_base_units,
        credited_fiat_amount=invoice.credited_fiat_amount,
        excess_base_units=invoice.excess_base_units,
    )


def terms_of(invoice: Invoice, *, asset: AssetConfiguration, minimum_refund_usd: Decimal) -> InvoiceTerms:
    return InvoiceTerms(
        fiat_amount=invoice.fiat_amount,
        crypto_amount_base_units=invoice.crypto_amount_base_units,
        accuracy_percent=invoice.accuracy_percent,
        decimals=asset.decimals,
        quote_decimals=asset.quote_decimals,
        minimum_refund_usd=minimum_refund_usd,
    )


def list_refunds_in_asset(operations: list[OperationValue], *, asset_symbol: str | None) -> list[OperationValue]:
    """Refunds in one asset. The invoice's own refunds are those in its asset; a stray payment sent back is in
    another one, and its base units must never mix into the invoice's (INC-001, 2026-10-11)."""
    return [
        operation
        for operation in operations
        if operation.kind == OperationKind.REFUND and operation.asset_symbol == asset_symbol
    ]


def refunded_by_sender(operations: list[OperationValue], *, asset_symbol: str | None) -> dict[str, int]:
    """Refunds in the asset that moved or may still move money, per recipient."""
    totals: dict[str, int] = {}
    for operation in list_refunds_in_asset(operations, asset_symbol=asset_symbol):
        # Only a proven failure (a reverted receipt) frees the amount; needs_owner keeps it reserved.
        if operation.status == OperationStatus.FAILED:
            continue
        if operation.recipient_address is None:
            continue
        totals[operation.recipient_address] = totals.get(operation.recipient_address, 0) + (
            operation.amount_base_units or 0
        )
    return totals


def compute_refundable_split(snapshot: InvoiceSnapshot) -> RefundableSplit:
    invoice = snapshot.invoice
    match invoice.status:
        case InvoiceStatus.OVERPAID:
            filled = invoice.received_base_units - invoice.excess_base_units
        case InvoiceStatus.UNDERPAID:
            filled = 0
        case (
            InvoiceStatus.AWAITING_ASSET
            | InvoiceStatus.AWAITING_PAYMENT
            | InvoiceStatus.CONFIRMING
            | InvoiceStatus.PAID
            | InvoiceStatus.EXPIRED
            | InvoiceStatus.CANCELLED
        ):
            filled = invoice.received_base_units
    return split_refundable(
        [
            PaymentShare(
                sender=payment.sender,
                sender_has_code=payment.sender_has_code,
                amount_base_units=payment.amount_base_units,
            )
            for payment in snapshot.payments
            if payment.final
        ],
        received_base_units=invoice.received_base_units,
        filled_base_units=filled,
        refunded_by_sender=refunded_by_sender(snapshot.operations, asset_symbol=invoice.asset_symbol),
    )


def is_at_least_minimum(base_units: int, *, snapshot: InvoiceSnapshot) -> bool:
    return is_asset_amount_at_least_minimum(base_units, asset=snapshot.asset, snapshot=snapshot)


def is_asset_amount_at_least_minimum(
    base_units: int, *, asset: AssetConfiguration | None, snapshot: InvoiceSnapshot
) -> bool:
    """False without a price too: callers that decide something once check ``has_usd_price`` first, since a
    missing price means "decide later", never "below the minimum" (2026-10-12)."""
    price_usd = find_usd_price(asset, snapshot=snapshot)
    if asset is None or price_usd is None:
        return False
    return whole_units(base_units, decimals=asset.decimals) * price_usd >= snapshot.minimum_refund_usd


def find_usd_price(asset: AssetConfiguration | None, *, snapshot: InvoiceSnapshot) -> Decimal | None:
    if asset is None or snapshot.prices is None:
        return None
    return snapshot.prices.asset_prices_usd.get(asset.symbol)


def has_usd_price(asset: AssetConfiguration | None, *, snapshot: InvoiceSnapshot) -> bool:
    return find_usd_price(asset, snapshot=snapshot) is not None


def pays_the_operator(
    invoice: Invoice, *, operator_account_ids: tuple[int, ...], operator_destination_address: str
) -> bool:
    """Whether the invoice is one of the operator's own accounts' (``PAY_OPERATOR_ACCOUNT_IDS``) and its money goes to
    the operator's own address (``PAY_DEFAULT_DESTINATION_ADDRESS``). Only then may the operator float refund anything
    on it: a float refund on top of a sweep to someone else's address would be free money from the float, and an
    invoice anyone else made to the operator's address would let them spend the float's gas on refund after refund.
    Every refund path checks this."""
    return (
        invoice.account_id is not None
        and invoice.account_id in operator_account_ids
        and bool(operator_destination_address)
        and invoice.destination_address == operator_destination_address
    )


def is_underpaid_within_payment_window(invoice: Invoice, *, now: datetime) -> bool:
    """An underpaid amount can be asked back only after ``expires_at``: a refund closes
    the invoice, so before that anyone could drain the float or close a payer's open invoice."""
    return invoice.status == InvoiceStatus.UNDERPAID and invoice.expires_at is not None and now < invoice.expires_at


def present_refund_offer(snapshot: InvoiceSnapshot) -> InvoiceRefundOffer:
    """Available senders, or the reason none is (PLAN.md section 7.1)."""
    invoice = snapshot.invoice
    asset = snapshot.asset

    def unavailable(reason: RefundUnavailableReason) -> InvoiceRefundOffer:
        return InvoiceRefundOffer(available=False, reason=reason, senders=[])

    if asset is None:
        return unavailable(RefundUnavailableReason.NOT_OVERPAID)
    refunds = list_refunds_in_asset(snapshot.operations, asset_symbol=invoice.asset_symbol)
    blocking_reason = find_refund_offer_blocking_reason(snapshot, refunds=refunds)
    if blocking_reason is not None:
        return unavailable(blocking_reason)
    split = compute_refundable_split(snapshot)
    positive = [sender for sender in split.senders if sender.refundable_base_units > 0]
    offerable = [sender for sender in positive if not sender.sender_has_code]
    if offerable and not has_usd_price(asset, snapshot=snapshot):
        # Without a price the minimum is unknown, not missed (2026-10-13).
        return unavailable(RefundUnavailableReason.PRICE_UNAVAILABLE)
    above_minimum = [
        sender for sender in offerable if is_at_least_minimum(sender.refundable_base_units, snapshot=snapshot)
    ]
    if above_minimum:
        return InvoiceRefundOffer(
            available=True,
            reason=None,
            senders=[
                RefundOfferSender(
                    sender=sender.sender,
                    amount=format_base_units(sender.refundable_base_units, decimals=asset.decimals),
                )
                for sender in above_minimum
            ],
        )
    return unavailable(
        find_refund_offer_reason_without_sender(
            split, has_offerable_sender=bool(offerable), has_positive_sender=bool(positive), refunds=refunds
        )
    )


def find_refund_offer_blocking_reason(
    snapshot: InvoiceSnapshot, *, refunds: list[OperationValue]
) -> RefundUnavailableReason | None:
    """Why no refund is offered before any sender is weighed, or None."""
    invoice = snapshot.invoice
    if invoice.status not in {InvoiceStatus.OVERPAID, InvoiceStatus.UNDERPAID}:
        return RefundUnavailableReason.NOT_OVERPAID
    if not snapshot.float_refunds_allowed:
        return RefundUnavailableReason.RECEIVER_SETTLES
    if is_underpaid_within_payment_window(invoice, now=datetime.now(UTC)):
        return RefundUnavailableReason.PAYMENT_WINDOW_OPEN
    if any(operation.status.is_active for operation in refunds):
        return RefundUnavailableReason.REFUND_IN_PROGRESS
    if invoice.status == InvoiceStatus.OVERPAID and invoice.automatic_refund_deferred_since is not None:
        # The excess goes back by itself once a price is readable (2026-10-12).
        return RefundUnavailableReason.REFUND_IN_PROGRESS
    return None


def find_refund_offer_reason_without_sender(
    split: RefundableSplit, *, has_offerable_sender: bool, has_positive_sender: bool, refunds: list[OperationValue]
) -> RefundUnavailableReason:
    """Why no sender is offered once every sender is weighed."""
    if has_offerable_sender:
        return RefundUnavailableReason.BELOW_MINIMUM
    if has_positive_sender:
        return RefundUnavailableReason.CONTRACT_SENDER
    if any(operation.status == OperationStatus.FINAL for operation in refunds):
        return RefundUnavailableReason.ALREADY_REFUNDED
    if split.unknown_sender_base_units > 0:
        return RefundUnavailableReason.UNKNOWN_SENDER
    return RefundUnavailableReason.NOT_OVERPAID


def current_price_in_fiat(snapshot: InvoiceSnapshot) -> Decimal | None:
    """Fiat units per asset unit for the remainder: the live quote's, else the last quote's (a lapsed quote still
    shows what was asked; the payer asks for a fresh one)."""
    if snapshot.latest_quote is not None:
        return snapshot.latest_quote.asset_price_in_fiat
    return None


def present_fiat_amount(fiat_amount: Decimal, *, fiat_currency: str, eur_price_usd: Decimal | None) -> InvoiceFiat:
    """A USD or EUR amount with its USD and EUR worth (the other one null without a EUR price); also a quantity
    link's amount (payments/payment_links)."""
    usd = fiat_amount if fiat_currency == "USD" else None
    if fiat_currency == "EUR" and eur_price_usd is not None:
        usd = fiat_amount * eur_price_usd
    eur = fiat_amount if fiat_currency == "EUR" else None
    if fiat_currency == "USD" and eur_price_usd is not None:
        eur = fiat_amount / eur_price_usd
    return InvoiceFiat(
        currency=fiat_currency,
        amount=str(fiat_amount.quantize(CENT)),
        usd=str(usd.quantize(CENT, ROUND_HALF_UP)) if usd is not None else None,
        eur=str(eur.quantize(CENT, ROUND_HALF_UP)) if eur is not None else None,
    )


def present_fiat(snapshot: InvoiceSnapshot, *, amount_due_base_units: int | None) -> InvoiceFiat:
    invoice = snapshot.invoice
    prices = snapshot.prices
    if invoice.fiat_currency is not None and invoice.fiat_amount is not None:
        quote = snapshot.latest_quote
        eur_price_usd = quote.eur_price_usd if quote is not None else (prices.eur_price_usd if prices else None)
        return present_fiat_amount(
            invoice.fiat_amount, fiat_currency=invoice.fiat_currency, eur_price_usd=eur_price_usd
        )
    asset = snapshot.asset
    if prices is None or asset is None or amount_due_base_units is None:
        return InvoiceFiat(currency=None, amount=None, usd=None, eur=None)
    usd_value = whole_units(amount_due_base_units, decimals=asset.decimals) * prices.asset_prices_usd[asset.symbol]
    return InvoiceFiat(
        currency=None,
        amount=None,
        usd=str(usd_value.quantize(CENT, ROUND_HALF_UP)),
        eur=str((usd_value / prices.eur_price_usd).quantize(CENT, ROUND_HALF_UP)),
    )


def present_sweeps(snapshot: InvoiceSnapshot) -> list[InvoiceSweep]:
    """Every final sweep in every asset (a stray coin's too, INC-001), then the gateway's sweeps under way or
    failed."""
    asset_by_token = {asset.token_address: asset for asset in snapshot.chain.assets}
    views = [
        InvoiceSweep(
            tx_hash=event.transaction_hash,
            asset_symbol=asset.symbol,
            amount=format_base_units(event.amount_base_units, decimals=asset.decimals),
            status=InvoiceSweepStatus.FINAL,
        )
        for event in snapshot.sweep_events
        if (asset := asset_by_token.get(event.token_address)) is not None
    ]
    views.extend(
        InvoiceSweep(
            tx_hash=operation.transaction_hash,
            asset_symbol=None,
            amount=None,
            status=InvoiceSweepStatus(operation.status.value),
        )
        for operation in snapshot.operations
        if operation.kind == OperationKind.SWEEP and operation.status.is_active
    )
    views.extend(
        InvoiceSweep(
            tx_hash=operation.transaction_hash, asset_symbol=None, amount=None, status=InvoiceSweepStatus.FAILED
        )
        for operation in snapshot.operations
        # A frozen key's sweep, ended so another key sweeps (2026-10-13), failed nothing the payer or account should see.
        if operation.kind == OperationKind.SWEEP
        and operation.status == OperationStatus.FAILED
        and not operation.is_sweep_for_another_key
    )
    return views


def is_superseded_refund_failure(operation: OperationValue, *, later_refunds: list[OperationValue]) -> bool:
    """A failed refund followed by an attempt to the same sender in the same coin that was sent or is under way."""
    return operation.status == OperationStatus.FAILED and any(
        later_refund.status == OperationStatus.FINAL or later_refund.status.is_active
        for later_refund in later_refunds
        if (later_refund.recipient_address or "").casefold() == (operation.recipient_address or "").casefold()
        and later_refund.asset_symbol == operation.asset_symbol
    )


def present_refunds(snapshot: InvoiceSnapshot, *, hide_superseded_failures: bool) -> list[InvoiceRefund]:
    """Every refund attempt, oldest first. The payer's view (``hide_superseded_failures``) shows only outcomes: a failed
    attempt that a later one to the same sender and coin replaced is left out; one with nothing after it stays, the
    receiver follows it up. The account and the operator see every attempt (2026-10-14)."""
    refund_operations = [operation for operation in snapshot.operations if operation.kind == OperationKind.REFUND]
    views: list[InvoiceRefund] = []
    for index, operation in enumerate(refund_operations):
        if hide_superseded_failures and is_superseded_refund_failure(
            operation, later_refunds=refund_operations[index + 1 :]
        ):
            continue
        asset = snapshot.chain.find_asset(operation.asset_symbol or "")
        views.append(
            InvoiceRefund(
                tx_hash=operation.transaction_hash,
                recipient=operation.recipient_address,
                asset_symbol=asset.symbol if asset is not None else operation.asset_symbol,
                amount=format_base_units(operation.amount_base_units or 0, decimals=asset.decimals if asset else 18),
                status=operation.status,
            )
        )
    return views


def decide_stray_refund_handling(
    stray_payment: InvoiceStrayPayment, *, snapshot: InvoiceSnapshot, sender_total_base_units: int
) -> StrayRefundHandling:
    """The same rules as an overpay's excess: back by itself only on an invoice paying
    the operator, to a plain-account sender, when what that sender sent in the asset is worth the minimum refund."""
    if not snapshot.float_refunds_allowed:
        return StrayRefundHandling.RECEIVER_SETTLES
    sender_has_code = any(row.sender_has_code for row in snapshot.stray_payments if row.sender == stray_payment.sender)
    if stray_payment.sender is None or sender_has_code:
        return StrayRefundHandling.OWNER_SETTLES
    asset = snapshot.chain.find_asset(stray_payment.asset_symbol)
    if not has_usd_price(asset, snapshot=snapshot):
        # No price, no verdict of our own: the watcher's is the one that stands. It said below the minimum only if it
        # decided (nothing deferred, every row final) and sent nothing back (2026-10-12).
        decided_without_refund = (
            snapshot.invoice.automatic_refund_deferred_since is None
            and all(
                row.final
                for row in snapshot.stray_payments
                if row.sender == stray_payment.sender and row.asset_symbol == stray_payment.asset_symbol
            )
            and not any(
                operation.recipient_address == stray_payment.sender
                for operation in list_refunds_in_asset(snapshot.operations, asset_symbol=stray_payment.asset_symbol)
            )
        )
        return StrayRefundHandling.BELOW_MINIMUM if decided_without_refund else StrayRefundHandling.AUTOMATIC
    if not is_asset_amount_at_least_minimum(sender_total_base_units, asset=asset, snapshot=snapshot):
        return StrayRefundHandling.BELOW_MINIMUM
    return StrayRefundHandling.AUTOMATIC


def present_stray_payments(snapshot: InvoiceSnapshot) -> list[InvoicePublicStrayPayment]:
    """Each stray row, ``swept`` once the finalized sweeps in its asset cover it and everything before it, and what
    becomes of it (``refund_handling``)."""
    total_by_sender_and_symbol: dict[tuple[str | None, str], int] = {}
    for stray_payment in snapshot.stray_payments:
        key = (stray_payment.sender, stray_payment.asset_symbol)
        total_by_sender_and_symbol[key] = total_by_sender_and_symbol.get(key, 0) + stray_payment.amount_base_units
    swept_by_token: dict[str | None, int] = {}
    for event in snapshot.sweep_events:
        swept_by_token[event.token_address] = swept_by_token.get(event.token_address, 0) + event.amount_base_units
    running_total_by_symbol: dict[str, int] = {}
    views: list[InvoicePublicStrayPayment] = []
    for stray_payment in snapshot.stray_payments:
        asset = snapshot.chain.find_asset(stray_payment.asset_symbol)
        if asset is None:
            continue
        running_total = running_total_by_symbol.get(asset.symbol, 0) + stray_payment.amount_base_units
        running_total_by_symbol[asset.symbol] = running_total
        views.append(
            InvoicePublicStrayPayment(
                asset_symbol=asset.symbol,
                amount=format_base_units(stray_payment.amount_base_units, decimals=asset.decimals),
                sender=stray_payment.sender,
                tx_hash=stray_payment.transaction_hash,
                block_number=stray_payment.block_number,
                final=stray_payment.final,
                swept=swept_by_token.get(asset.token_address, 0) >= running_total,
                refund_handling=decide_stray_refund_handling(
                    stray_payment,
                    snapshot=snapshot,
                    sender_total_base_units=total_by_sender_and_symbol[
                        (stray_payment.sender, stray_payment.asset_symbol)
                    ],
                ),
            )
        )
    return views


class StraySenderRefundable(BaseModel):
    """What one sender may get back in one stray asset: its final stray money minus refunds that moved or may move."""

    model_config = ConfigDict(frozen=True)

    sender: str | None
    sender_has_code: bool
    asset: AssetConfiguration
    refundable_base_units: int


def compute_stray_refundables(snapshot: InvoiceSnapshot) -> list[StraySenderRefundable]:
    """Per sender and asset, final stray rows only; an unknown sender (no transaction explains the money) is one
    group of its own and is never refunded."""
    final_by_sender_and_symbol: dict[tuple[str | None, str], int] = {}
    has_code_by_sender: dict[str, bool] = {}
    for stray_payment in snapshot.stray_payments:
        if not stray_payment.final:
            continue
        key = (stray_payment.sender, stray_payment.asset_symbol)
        final_by_sender_and_symbol[key] = final_by_sender_and_symbol.get(key, 0) + stray_payment.amount_base_units
        if stray_payment.sender is not None:
            has_code_by_sender[stray_payment.sender] = (
                has_code_by_sender.get(stray_payment.sender, False) or stray_payment.sender_has_code
            )
    refundables: list[StraySenderRefundable] = []
    for (sender, asset_symbol), amount in final_by_sender_and_symbol.items():
        asset = snapshot.chain.find_asset(asset_symbol)
        if asset is None:
            continue
        refunded = refunded_by_sender(snapshot.operations, asset_symbol=asset.symbol).get(sender or "", 0)
        refundables.append(
            StraySenderRefundable(
                sender=sender,
                sender_has_code=has_code_by_sender.get(sender or "", False),
                asset=asset,
                refundable_base_units=max(0, amount - refunded),
            )
        )
    return refundables


def present_invoice_payment_options(snapshot: InvoiceSnapshot) -> list[PaymentOption]:
    """Every coin on every offered chain with what it would ask now, for an awaiting fiat invoice; empty once the
    coin is picked, for a crypto invoice, and (per chain) without an answering feed."""
    invoice = snapshot.invoice
    if invoice.status != InvoiceStatus.AWAITING_ASSET or invoice.fiat_currency is None or invoice.fiat_amount is None:
        return []
    return present_payment_options(
        snapshot.offered_chains,
        offered_assets_by_chain=offered_assets_by_chain_of(invoice),
        prices_by_chain=snapshot.option_prices_by_chain,
        fiat_amount=invoice.fiat_amount,
        fiat_currency=invoice.fiat_currency,
    )


def present_monero_confirmations(snapshot: InvoiceSnapshot) -> tuple[int, int] | None:
    """(confirmations, confirmations_final) of a Monero invoice's best payment: its newest credited one, the one the
    invoice waits on (every older one is at least as deep), at the confirmed tip the watcher last stored
    (``latest_reading_block_number``, the tip final counting reads). None on an EVM invoice and while nothing is
    credited."""
    chain = snapshot.chain
    if not isinstance(chain, MoneroChainConfiguration):
        return None
    credited_heights = [
        monero_payment.block_height
        for monero_payment in snapshot.monero_payments
        if is_credited(monero_payment) and monero_payment.block_height is not None
    ]
    if not credited_heights:
        return None
    newest_credited = next(
        monero_payment
        for monero_payment in snapshot.monero_payments
        if is_credited(monero_payment) and monero_payment.block_height == max(credited_heights)
    )
    confirmations = count_confirmations(newest_credited, tip_height=snapshot.invoice.latest_reading_block_number)
    return confirmations, invoice_confirmations_final(snapshot.invoice, chain=chain)


def present_monero_invoice(snapshot: InvoiceSnapshot) -> InvoiceMoneroDetails | None:
    """The account's Monero block: None on an EVM invoice; on a Monero one the confirmations are 0 while nothing is
    credited."""
    invoice = snapshot.invoice
    chain = snapshot.chain
    if invoice.subaddress_minor is None or not isinstance(chain, MoneroChainConfiguration):
        return None
    confirmations = present_monero_confirmations(snapshot)
    return InvoiceMoneroDetails(
        confirmations=confirmations[0] if confirmations is not None else 0,
        confirmations_final=invoice_confirmations_final(invoice, chain=chain),
    )


def present_monero_payments(snapshot: InvoiceSnapshot, *, asset: AssetConfiguration) -> list[InvoicePublicPayment]:
    """A Monero invoice's credited outputs as payments: the txid, no sender (ring signatures hide it), the height, and
    ``final`` once counted at final. Pool and held outputs are not payments yet."""
    chain = snapshot.chain
    if not isinstance(chain, MoneroChainConfiguration):
        return []
    return [
        InvoicePublicPayment(
            tx_hash=monero_payment.txid,
            sender=None,
            amount=format_base_units(monero_payment.amount_piconero, decimals=asset.decimals),
            block_number=monero_payment.block_height,
            final=is_counted_at_final(
                monero_payment,
                tip_height=snapshot.invoice.latest_reading_block_number,
                confirmations_final=invoice_confirmations_final(snapshot.invoice, chain=chain),
            ),
        )
        for monero_payment in snapshot.monero_payments
        if is_credited(monero_payment) and monero_payment.block_height is not None
    ]


def compute_public_amount_due_and_remaining(snapshot: InvoiceSnapshot) -> tuple[int | None, int | None]:
    """(amount due, remaining) in base units; both None until a coin is picked."""
    invoice = snapshot.invoice
    asset = snapshot.asset
    if asset is None:
        return None, None
    remaining = remaining_base_units(
        accounting_state_of(invoice),
        terms=terms_of(invoice, asset=asset, minimum_refund_usd=snapshot.minimum_refund_usd),
        asset_price_in_fiat=current_price_in_fiat(snapshot),
    )
    if invoice.status.is_settled_by_payment:
        return invoice.amount_due_base_units, remaining
    if remaining is not None:
        return invoice.received_base_units + remaining, remaining
    return None, remaining


def format_optional_base_units(base_units: int | None, *, asset: AssetConfiguration | None) -> str | None:
    """``format_base_units`` in the asset's decimals, or None without an asset or an amount."""
    if asset is None or base_units is None:
        return None
    return format_base_units(base_units, decimals=asset.decimals)


def present_public_payments(snapshot: InvoiceSnapshot) -> list[InvoicePublicPayment]:
    """The invoice's payments in its asset, then a Monero invoice's credited outputs; none until a coin is picked."""
    asset = snapshot.asset
    if asset is None:
        return []
    return [
        InvoicePublicPayment(
            tx_hash=payment.transaction_hash,
            sender=payment.sender,
            amount=format_base_units(payment.amount_base_units, decimals=asset.decimals),
            block_number=payment.block_number,
            final=payment.final,
        )
        for payment in snapshot.payments
    ] + present_monero_payments(snapshot, asset=asset)


def present_seller_support_contact(contact: SellerSupportContact | None) -> InvoiceSellerSupportContact | None:
    if contact is None:
        return None
    return InvoiceSellerSupportContact(kind=contact.kind, display_text=contact.display_text, href=contact.href)


def present_public_invoice(snapshot: InvoiceSnapshot) -> InvoicePublicResponse:
    invoice = snapshot.invoice
    asset = snapshot.asset
    chain = snapshot.chain
    amount_due, remaining = compute_public_amount_due_and_remaining(snapshot)
    quote = snapshot.latest_quote
    show_payment = asset is not None and invoice.status != InvoiceStatus.AWAITING_ASSET
    payment_asset = asset if show_payment else None
    monero_confirmations = present_monero_confirmations(snapshot)
    return InvoicePublicResponse(
        id=invoice.id,
        url=f"{snapshot.public_base_url}/i/{invoice.id}",
        status=invoice.status,
        partial=invoice.partial,
        late_payment=invoice.late_payment,
        sweep_blocked=invoice.sweep_blocked,
        chain=present_chain(chain),
        asset=present_asset(payment_asset) if payment_asset is not None else None,
        assets=invoice.assets,
        payment_options=present_invoice_payment_options(snapshot),
        deposit_address=invoice.deposit_address if show_payment else None,
        amount_due=format_optional_base_units(amount_due, asset=payment_asset),
        amount_received=format_optional_base_units(invoice.received_base_units, asset=payment_asset),
        amount_remaining=format_optional_base_units(remaining, asset=payment_asset),
        fiat=present_fiat(snapshot, amount_due_base_units=amount_due),
        quote=InvoiceQuote(asset_price_usd=format(quote.asset_price_usd.normalize(), "f"), expires_at=quote.expires_at)
        if show_payment and quote is not None
        else None,
        note=invoice.note,
        return_url=invoice.return_url,
        success_url=invoice.success_url,
        expires_at=invoice.expires_at,
        underpaid_open_until=invoice.underpaid_open_until,
        payment_uri=build_payment_uri(
            chain=chain,
            asset=payment_asset,
            deposit_address=require_deposit_address(invoice),
            amount=remaining,
            note=invoice.note,
        )
        if payment_asset is not None and remaining is not None
        else None,
        payments=present_public_payments(snapshot),
        sweeps=present_sweeps(snapshot),
        refunds=present_refunds(snapshot, hide_superseded_failures=True),
        refund_offer=present_refund_offer(snapshot),
        stray_payments=present_stray_payments(snapshot),
        seen=any(
            is_seen_by_payer(monero_payment, now=datetime.now(UTC)) for monero_payment in snapshot.monero_payments
        ),
        confirmations=monero_confirmations[0] if monero_confirmations is not None else None,
        confirmations_final=monero_confirmations[1] if monero_confirmations is not None else None,
        seller_support_contact=present_seller_support_contact(snapshot.seller_support_contact),
        created_at=invoice.created_at,
        updated_at=invoice.updated_at,
    )


def present_account_invoice(snapshot: InvoiceSnapshot) -> InvoiceAccountResponse:
    """The public view's fields, each named, then the account's own: every field of both views is required, so one
    added to the public view and left out here is a type error, not a silent default on the account view and its
    webhooks."""
    invoice = snapshot.invoice
    public_invoice_response = present_public_invoice(snapshot)
    return InvoiceAccountResponse(
        id=public_invoice_response.id,
        url=public_invoice_response.url,
        status=public_invoice_response.status,
        partial=public_invoice_response.partial,
        late_payment=public_invoice_response.late_payment,
        sweep_blocked=public_invoice_response.sweep_blocked,
        chain=public_invoice_response.chain,
        asset=public_invoice_response.asset,
        assets=public_invoice_response.assets,
        payment_options=public_invoice_response.payment_options,
        deposit_address=public_invoice_response.deposit_address,
        amount_due=public_invoice_response.amount_due,
        amount_received=public_invoice_response.amount_received,
        amount_remaining=public_invoice_response.amount_remaining,
        fiat=public_invoice_response.fiat,
        quote=public_invoice_response.quote,
        note=public_invoice_response.note,
        return_url=public_invoice_response.return_url,
        success_url=public_invoice_response.success_url,
        expires_at=public_invoice_response.expires_at,
        underpaid_open_until=public_invoice_response.underpaid_open_until,
        payment_uri=public_invoice_response.payment_uri,
        payments=public_invoice_response.payments,
        sweeps=public_invoice_response.sweeps,
        # The account sees every refund attempt, the replaced failures too.
        refunds=present_refunds(snapshot, hide_superseded_failures=False),
        refund_offer=public_invoice_response.refund_offer,
        stray_payments=public_invoice_response.stray_payments,
        seen=public_invoice_response.seen,
        confirmations=public_invoice_response.confirmations,
        confirmations_final=public_invoice_response.confirmations_final,
        seller_support_contact=public_invoice_response.seller_support_contact,
        created_at=public_invoice_response.created_at,
        updated_at=public_invoice_response.updated_at,
        destination_address=invoice.destination_address,
        order_id=invoice.order_id,
        metadata=invoice.private_metadata,
        accuracy_percent=format(invoice.accuracy_percent.normalize(), "f"),
        payment_link_id=invoice.payment_link_id,
        invoice_schedule_id=invoice.invoice_schedule_id,
        invoice_schedule_period_start=invoice.invoice_schedule_period_start,
        paid_at=invoice.paid_at,
        callback_url=invoice.callback_url,
        refreshed_from_invoice_id=invoice.refreshed_from_invoice_id,
        refreshed_into_invoice_id=invoice.refreshed_into_invoice_id,
        monero=present_monero_invoice(snapshot),
    )


def encode_cursor(invoice: Invoice) -> str:
    return base64.urlsafe_b64encode(f"{invoice.created_at.isoformat()}|{invoice.id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        created_at_text, invoice_id = decode_url_safe_base64_cursor(cursor).split("|", 1)
        return datetime.fromisoformat(created_at_text), invoice_id
    except (ValueError, binascii.Error) as exc:
        raise InvalidInputError("cursor: not a cursor this API returned") from exc


def render_qr_svg(text: str) -> str:
    buffer = io.BytesIO()
    segno.make(text, error="m").save(buffer, kind="svg", scale=8, border=2, xmldecl=False, svgns=True)
    return buffer.getvalue().decode()
