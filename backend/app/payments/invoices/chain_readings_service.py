"""ChainReadingsService: what the watcher read, applied to invoices (PLAN.md sections 4 and 5, step 4).

Every method runs on the caller's session and transaction; status changes, sweep requests and account events commit
together. Invoices are locked in id order. Gross received at the finalized block B is the deposit's balance at B
plus every finalized ``DepositSwept`` amount for that deposit and asset, so a sweep (anyone's) never lowers it.
"""

from datetime import UTC, datetime
from typing import Self

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.deposit_factory_api.schemas import DepositSweptEvent
from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.schemas import BlockHead
from app.payments.chains.types import AssetConfiguration
from app.payments.invoices.accounting_service import CreditPrices, apply_credit
from app.payments.invoices.enums import ChainTransactionState, InvoiceEventType, InvoiceStatus
from app.payments.invoices.errors import StrayTransactionRefusedError
from app.payments.invoices.log_events import (
    AutomaticRefundDecidedAfterDeferral,
    AutomaticRefundDeferred,
    LatePaymentFound,
    SenderLessStrayRowAttributed,
    StrayPaymentFinal,
    StrayPaymentRecordedFromTransaction,
)
from app.payments.invoices.models import Invoice, InvoiceStrayPayment, require_deposit_address
from app.payments.invoices.presenters import accounting_state_of, terms_of
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.service import InvoiceService
from app.payments.invoices.transaction_readings import read_chain_transaction
from app.payments.invoices.types import (
    AttributedPayment,
    AttributedStrayPayment,
    CanonicalTransactionPayments,
    ChainTransactionReading,
    DepositBalances,
    DepositCreationPosition,
    DepositCreationRange,
    DepositInCreationOrder,
    FinalPaymentBlock,
    RescannedDeposit,
    UnfinalPayment,
    WatchedDeposit,
)
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.settlements.enums import OperationKind, OperationStatus
from app.payments.settlements.types import OperationValue, SettlementOutcome
from app.shared import logging as log

logger = log.get_logger(__name__)

STATUS_EVENTS = {
    InvoiceStatus.UNDERPAID: InvoiceEventType.UNDERPAID,
    InvoiceStatus.PAID: InvoiceEventType.PAID,
    InvoiceStatus.OVERPAID: InvoiceEventType.OVERPAID,
}


class ChainReadingsService:
    def __init__(self, *, repository: InvoiceRepository, invoice_service: InvoiceService) -> None:
        self.repository = repository
        self.invoice_service = invoice_service
        self.chains_service = invoice_service.chains_service

    @classmethod
    def on_session(cls, session: AsyncSession, *, redis: Redis, wiring: InvoiceWiring) -> Self:
        return cls(
            repository=InvoiceRepository(session=session),
            invoice_service=InvoiceService.on_session(session, redis=redis, wiring=wiring),
        )

    # What to read

    async def list_watched_deposits(self, chain_key: str) -> list[WatchedDeposit]:
        chain = self.chains_service.get_chain(chain_key)
        invoices = await self.repository.list_watched_invoices(chain_key)
        swept_by_deposit_token: dict[tuple[str, str | None], int] = {}
        for event in await self.repository.list_sweep_events(
            chain_key, deposit_addresses=[require_deposit_address(invoice) for invoice in invoices]
        ):
            key = (event.deposit_address, event.token_address)
            swept_by_deposit_token[key] = swept_by_deposit_token.get(key, 0) + event.amount_base_units
        watched: list[WatchedDeposit] = []
        for invoice in invoices:
            asset = chain.find_asset(invoice.asset_symbol or "")
            if asset is None:
                continue
            watched.append(
                WatchedDeposit(
                    invoice_id=invoice.id,
                    deposit_address=require_deposit_address(invoice),
                    asset_symbol=asset.symbol,
                    token_address=asset.token_address,
                    latest_balance_base_units=invoice.latest_balance_base_units,
                    latest_reading_block_number=invoice.latest_reading_block_number,
                    received_base_units=invoice.received_base_units,
                    swept_base_units=swept_by_deposit_token.get(
                        (require_deposit_address(invoice), asset.token_address), 0
                    ),
                    latest_native_balance_base_units=invoice.latest_native_balance_base_units
                    if asset.token_address is not None
                    else None,
                )
            )
        return watched

    async def count_deposits_in_creation_range(self, chain_key: str, *, creation_range: DepositCreationRange) -> int:
        return await self.repository.count_deposits_in_creation_range(chain_key, creation_range=creation_range)

    async def list_deposits_in_creation_order(
        self,
        chain_key: str,
        *,
        creation_range: DepositCreationRange,
        after_position: DepositCreationPosition | None,
        limit: int,
    ) -> list[DepositInCreationOrder]:
        """One page of a rescan tier's pass (chain_watcher/rescan_service.py), in (created_at, id) order."""
        return [
            DepositInCreationOrder(invoice_id=invoice_id, deposit_address=deposit_address, created_at=created_at)
            for invoice_id, deposit_address, created_at in await self.repository.list_deposits_in_creation_order(
                chain_key, creation_range=creation_range, after_position=after_position, limit=limit
            )
        ]

    # Payments (attribution, PLAN.md section 5 step 5)

    async def record_payments(
        self, payments: list[AttributedPayment], *, senders_with_code: set[str], finalized_block_number: int
    ) -> None:
        await self.repository.insert_payments(
            payments, senders_with_code=senders_with_code, finalized_block_number=finalized_block_number
        )

    async def list_unfinal_payments(self, chain_key: str, *, up_to_block_number: int) -> list[UnfinalPayment]:
        chain = self.chains_service.get_chain(chain_key)
        invoices = {invoice.id: invoice for invoice in await self.repository.list_unclosed_invoices(chain_key)}
        unfinal: list[UnfinalPayment] = []
        for payment in await self.repository.list_unfinal_payments_up_to(
            sorted(invoices), block_number=up_to_block_number
        ):
            invoice = invoices[payment.invoice_id]
            asset = chain.find_asset(invoice.asset_symbol or "")
            unfinal.append(
                UnfinalPayment(
                    payment_id=payment.id,
                    invoice_id=payment.invoice_id,
                    asset_symbol=invoice.asset_symbol or "",
                    transaction_hash=payment.transaction_hash,
                    log_index=payment.log_index,
                    sender=payment.sender,
                    amount_base_units=payment.amount_base_units,
                    block_number=payment.block_number,
                    block_hash=payment.block_hash,
                    deposit_address=require_deposit_address(invoice),
                    token_address=asset.token_address if asset is not None else None,
                )
            )
        return unfinal

    async def settle_payment_finality(
        self,
        *,
        confirmed: dict[int, FinalPaymentBlock],
        canonical_transactions: list[CanonicalTransactionPayments],
        senders_with_code: set[str],
    ) -> None:
        """Mark payments still at their block final, with the canonical header's timestamp; a transaction that moved
        or vanished has its rows replaced by what its canonical receipt shows, so one transfer is one row at its
        canonical block and log index."""
        for payment_id, final_block in confirmed.items():
            await self.repository.mark_payment_final(
                payment_id,
                block_number=final_block.block_number,
                block_hash=final_block.block_hash,
                block_timestamp=final_block.block_timestamp,
            )
        for transaction in canonical_transactions:
            await self.repository.replace_transaction_payments(
                transaction.invoice_id,
                transaction_hash=transaction.transaction_hash,
                payments=transaction.payments,
                senders_with_code=senders_with_code,
            )

    # Stray payments: money in an asset the invoice did not ask for (INC-001, 2026-10-11). Recorded and shown, swept
    # with the rest, never credited: nothing here touches the invoice's status or amounts.

    async def record_stray_payments(
        self, stray_payments: list[AttributedStrayPayment], *, senders_with_code: set[str], finalized_block_number: int
    ) -> None:
        invoice_ids = await self.repository.insert_stray_payments(
            stray_payments, senders_with_code=senders_with_code, finalized_block_number=finalized_block_number
        )
        await self.record_stray_payment_events(invoice_ids)

    async def record_stray_payment_events(self, invoice_ids: list[str]) -> None:
        """``invoice.stray_payment`` once a stray row is final, then the automatic refund to its sender where the
        rules allow one (its stray rows say how it is handled); a row reorged away before finality never alarms
        anyone."""
        for invoice in await self.repository.lock_invoices(sorted(set(invoice_ids))):
            log.emit(logger, event=StrayPaymentFinal(invoice_id=invoice.id))
            await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.STRAY_PAYMENT)
            self.defer_automatic_refunds(
                invoice, asset_symbols=await self.invoice_service.refund_stray_payments_automatically(invoice)
            )

    async def record_stray_payment_from_transaction(self, transaction_hash: str, *, chain_key: str) -> list[str]:
        """Record stray money the watcher never saw from its transaction hash (the operator's CLI; INC-001, where the
        deposit was swept before the watcher read other coins). The transaction must have succeeded and be final,
        and move a coin other than the invoice's into a known deposit: a direct native transfer, or a token Transfer
        log. Each such transfer becomes a final stray row with its sender, and the automatic refund follows. Never
        credits an invoice. Returns the invoice ids that got a row."""
        chain = self.chains_service.get_evm_chain(chain_key)
        reading = await read_chain_transaction(
            transaction_hash, rpc_client=self.invoice_service.wiring.rpc_clients[chain_key], chain=chain
        )
        if reading.state in {ChainTransactionState.UNKNOWN, ChainTransactionState.NOT_MINED}:
            raise StrayTransactionRefusedError("the chain does not show that transaction as mined")
        if reading.state == ChainTransactionState.FAILED:
            raise StrayTransactionRefusedError("that transaction reverted; it moved nothing")
        if not reading.final:
            raise StrayTransactionRefusedError("that transaction is not final yet; try again in a minute")
        invoice_ids = await self.list_invoice_ids_paid_by_transaction(reading, chain_key=chain_key)
        if not invoice_ids:
            raise StrayTransactionRefusedError("that transaction moved nothing into a deposit address")
        recorded_invoice_ids = await self.record_stray_transfers(reading, chain_key=chain_key, invoice_ids=invoice_ids)
        if not recorded_invoice_ids:
            raise StrayTransactionRefusedError(
                "that transaction is already recorded, or paid the invoice's own coin (the watcher credits that)"
            )
        return recorded_invoice_ids

    async def list_invoice_ids_paid_by_transaction(
        self, reading: ChainTransactionReading, *, chain_key: str
    ) -> list[str]:
        """The invoices whose deposit the transaction moved money into, in any token."""
        return sorted(
            invoice.id
            for invoice in await self.repository.list_invoices_by_deposit(
                chain_key, deposit_addresses=sorted({transfer.recipient for transfer in reading.transfers})
            )
        )

    async def record_stray_transfers(
        self, reading: ChainTransactionReading, *, chain_key: str, invoice_ids: list[str]
    ) -> list[str]:
        """Record the final transaction's transfers into the given invoices' deposits in a listed coin other than the
        invoice's as final stray rows, each once, and let the automatic refund follow; the operator's CLI passes every
        invoice the transaction paid, the payer's check only its own. Returns the invoice ids that got a new row."""
        locked_invoices = await self.repository.lock_invoices(sorted(set(invoice_ids)))
        invoice_by_deposit = {require_deposit_address(invoice): invoice for invoice in locked_invoices}
        existing_stray_rows = await self.repository.list_stray_payments([invoice.id for invoice in locked_invoices])
        recorded_keys = {
            (row.invoice_id, row.asset_symbol, row.log_index)
            for row in existing_stray_rows
            if row.transaction_hash == reading.transaction_hash
        }
        stray_payments: list[AttributedStrayPayment] = []
        for transfer in reading.transfers:
            invoice = invoice_by_deposit.get(transfer.recipient)
            if invoice is None or transfer.asset_symbol is None or transfer.asset_symbol == invoice.asset_symbol:
                continue
            if (invoice.id, transfer.asset_symbol, transfer.log_index) in recorded_keys:
                continue
            stray_payments.append(
                AttributedStrayPayment(
                    invoice_id=invoice.id,
                    asset_symbol=transfer.asset_symbol,
                    transaction_hash=reading.transaction_hash,
                    log_index=transfer.log_index,
                    transaction_index=reading.transaction_index,
                    sender=transfer.sender,
                    amount_base_units=transfer.amount_base_units,
                    block_number=reading.block_number,
                    block_hash=reading.block_hash,
                    block_timestamp=reading.block_timestamp,
                )
            )
        if not stray_payments:
            return []

        async def read_senders_with_code(endpoint: EvmRpcEndpoint) -> set[str]:
            return await endpoint.read_addresses_with_code(sorted({payment.sender or "" for payment in stray_payments}))

        senders_with_code = await self.invoice_service.wiring.rpc_clients[chain_key].call_with_failover(
            read_senders_with_code
        )
        for payment in stray_payments:
            log.emit(
                logger,
                event=StrayPaymentRecordedFromTransaction(
                    invoice_id=payment.invoice_id,
                    transaction_hash=reading.transaction_hash,
                    asset_symbol=payment.asset_symbol,
                ),
            )
        await self.absorb_sender_less_stray_rows(stray_payments, existing_stray_rows=existing_stray_rows)
        await self.record_stray_payments(
            stray_payments, senders_with_code=senders_with_code, finalized_block_number=reading.finalized_block_number
        )
        return sorted({payment.invoice_id for payment in stray_payments})

    async def absorb_sender_less_stray_rows(
        self, stray_payments: list[AttributedStrayPayment], *, existing_stray_rows: list[InvoiceStrayPayment]
    ) -> None:
        """A native rise the watcher could not match (its block scan reads at most ``NATIVE_ATTRIBUTION_MAXIMUM_BLOCKS``
        back, so a watcher stalled longer misses the transaction) is a row with no sender at the head it was read at.
        A transaction now proven to have sent that coin into the deposit at or before that head is what the row stood
        for: the row shrinks by its amount (gone at zero), so the money is counted once and its sender gets the
        refund instead of the owner settling it (2026-10-12)."""
        dropped_row_ids: list[int] = []
        for stray_payment in sorted(stray_payments, key=lambda payment: payment.block_number):
            left_to_absorb = stray_payment.amount_base_units
            for row in existing_stray_rows:
                if (
                    left_to_absorb <= 0
                    or row.id in dropped_row_ids
                    or row.invoice_id != stray_payment.invoice_id
                    or row.asset_symbol != stray_payment.asset_symbol
                    or row.transaction_hash is not None
                    or row.block_number < stray_payment.block_number
                ):
                    continue
                absorbed = min(left_to_absorb, row.amount_base_units)
                left_to_absorb -= absorbed
                row.amount_base_units -= absorbed
                if row.amount_base_units == 0:
                    dropped_row_ids.append(row.id)
                log.emit(
                    logger,
                    event=SenderLessStrayRowAttributed(
                        invoice_id=row.invoice_id,
                        asset_symbol=row.asset_symbol,
                        absorbed_base_units=str(absorbed),
                    ),
                )
        await self.repository.delete_stray_payments(dropped_row_ids)

    async def list_unfinal_stray_payments(self, chain_key: str, *, up_to_block_number: int) -> list[UnfinalPayment]:
        chain = self.chains_service.get_chain(chain_key)
        unfinal: list[UnfinalPayment] = []
        for stray_payment, deposit_address in await self.repository.list_unfinal_stray_payments_up_to(
            chain_key, block_number=up_to_block_number
        ):
            asset = chain.find_asset(stray_payment.asset_symbol)
            unfinal.append(
                UnfinalPayment(
                    payment_id=stray_payment.id,
                    invoice_id=stray_payment.invoice_id,
                    asset_symbol=stray_payment.asset_symbol,
                    transaction_hash=stray_payment.transaction_hash,
                    log_index=stray_payment.log_index,
                    sender=stray_payment.sender,
                    amount_base_units=stray_payment.amount_base_units,
                    block_number=stray_payment.block_number,
                    block_hash=stray_payment.block_hash,
                    deposit_address=deposit_address,
                    token_address=asset.token_address if asset is not None else None,
                )
            )
        return unfinal

    async def settle_stray_payment_finality(
        self,
        *,
        confirmed: dict[int, FinalPaymentBlock],
        canonical_transactions: list[CanonicalTransactionPayments],
        senders_with_code: set[str],
        dropped_stray_payment_ids: list[int],
    ) -> None:
        """As ``settle_payment_finality`` for stray rows; a row with no transaction whose block left the chain is
        dropped (the rise it stood for was reorged away)."""
        newly_final_invoice_ids: list[str] = []
        for stray_payment_id, final_block in confirmed.items():
            invoice_id = await self.repository.mark_stray_payment_final(
                stray_payment_id,
                block_number=final_block.block_number,
                block_hash=final_block.block_hash,
                block_timestamp=final_block.block_timestamp,
            )
            if invoice_id is not None:
                newly_final_invoice_ids.append(invoice_id)
        for transaction in canonical_transactions:
            rows_left = await self.repository.replace_transaction_stray_payments(
                transaction.invoice_id,
                asset_symbol=transaction.asset_symbol,
                transaction_hash=transaction.transaction_hash,
                stray_payments=[
                    AttributedStrayPayment(asset_symbol=transaction.asset_symbol, **payment.model_dump())
                    for payment in transaction.payments
                ],
                senders_with_code=senders_with_code,
            )
            if rows_left:
                newly_final_invoice_ids.append(transaction.invoice_id)
        await self.repository.delete_stray_payments(dropped_stray_payment_ids)
        await self.record_stray_payment_events(newly_final_invoice_ids)

    # Sweeps seen on chain

    async def record_deposit_sweep_events(self, chain_key: str, *, events: list[DepositSweptEvent]) -> None:
        """Store finalized DepositSwept events once; each invoice whose deposit got one is told ``invoice.swept``."""
        swept_deposits = await self.repository.insert_sweep_events(chain_key, events=events)
        invoices = await self.repository.list_invoices_by_deposit(chain_key, deposit_addresses=swept_deposits)
        for invoice in await self.repository.lock_invoices([invoice.id for invoice in invoices]):
            if invoice.sweep_blocked:
                invoice.sweep_blocked = False
            await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.SWEPT)

    # Balances (PLAN.md section 4)

    async def apply_balances(
        self, chain_key: str, *, balances: list[DepositBalances], finalized: BlockHead, latest: BlockHead
    ) -> None:
        chain = self.chains_service.get_chain(chain_key)
        invoices = {
            invoice.id: invoice for invoice in await self.repository.lock_invoices([b.invoice_id for b in balances])
        }
        sweep_events = await self.repository.list_sweep_events(
            chain_key, deposit_addresses=[require_deposit_address(invoice) for invoice in invoices.values()]
        )
        swept_by_deposit_token: dict[tuple[str, str | None], int] = {}
        for event in sweep_events:
            if event.block_number <= finalized.number:
                key = (event.deposit_address, event.token_address)
                swept_by_deposit_token[key] = swept_by_deposit_token.get(key, 0) + event.amount_base_units
        for reading in sorted(balances, key=lambda balance: balance.invoice_id):
            invoice = invoices.get(reading.invoice_id)
            if invoice is None or invoice.closed_at is not None or invoice.asset_symbol is None:
                continue
            asset = chain.find_asset(invoice.asset_symbol)
            if asset is None:
                continue
            swept = swept_by_deposit_token.get((require_deposit_address(invoice), asset.token_address), 0)
            await self.apply_deposit_reading(
                invoice,
                asset=asset,
                gross_finalized=reading.finalized_balance_base_units + swept,
                gross_latest=reading.latest_balance_base_units + swept,
                finalized_balance=reading.finalized_balance_base_units,
                finalized=finalized,
            )
            invoice.latest_balance_base_units = reading.latest_balance_base_units
            if reading.latest_native_balance_base_units is not None:
                invoice.latest_native_balance_base_units = reading.latest_native_balance_base_units
            invoice.latest_reading_block_number = latest.number

    async def apply_deposit_reading(
        self,
        invoice: Invoice,
        *,
        asset: AssetConfiguration,
        gross_finalized: int,
        gross_latest: int,
        finalized_balance: int,
        finalized: BlockHead,
    ) -> None:
        if gross_finalized > invoice.received_base_units:
            await self.credit_finalized_rise(invoice, asset=asset, gross_finalized=gross_finalized, finalized=finalized)
        if gross_latest > max(invoice.latest_gross_base_units, invoice.received_base_units):
            if invoice.status == InvoiceStatus.AWAITING_PAYMENT:
                invoice.status = InvoiceStatus.CONFIRMING
            await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.PAYMENT_DETECTED)
        elif invoice.status == InvoiceStatus.CONFIRMING and gross_latest <= invoice.received_base_units:
            # What `latest` showed is gone (a reorg before finality): nothing was received after all.
            invoice.status = InvoiceStatus.AWAITING_PAYMENT
        invoice.latest_gross_base_units = max(gross_latest, invoice.received_base_units)
        if invoice.status.is_settled_by_payment and not invoice.sweep_blocked:
            # The gateway's first sweep moves whatever was paid; once its own sweep is final, the deposit is swept
            # again only for more than dust. A third party's sweep does not count (2026-10-09, live: after one, a
            # payer's 0.3 POL top-up, under the 0.5 floor, was left in the deposit).
            if finalized_balance > 0 and (
                finalized_balance >= asset.minimum_sweep_base_units
                or not await self.invoice_service.settlement_service.has_final_sweep(invoice.id)
            ):
                await self.invoice_service.request_sweep(invoice)
            elif not await self.invoice_service.settlement_service.has_active_sweep(invoice.id):
                # Paid and the deposit is empty at a finalized block: settled; the rescan watches it from now on.
                invoice.closed_at = datetime.now(UTC)
        # Every finalized reading: final payments only count here, so the excess is final too.
        self.defer_automatic_refunds(
            invoice, asset_symbols=await self.invoice_service.refund_overpaid_excess_automatically(invoice)
        )

    async def credit_prices(
        self, invoice: Invoice, *, asset: AssetConfiguration, block_timestamp: int
    ) -> CreditPrices | None:
        """The quote live at the block's timestamp, else the feed price now; None when no price is readable."""
        block_time = datetime.fromtimestamp(block_timestamp, tz=UTC)
        quotes = await self.invoice_service.price_quote_service.list_quotes(invoice.id)
        live_quote = next((quote for quote in reversed(quotes) if quote.is_live_at(block_time)), None)
        if live_quote is not None:
            return CreditPrices(
                asset_price_in_fiat=live_quote.asset_price_in_fiat, asset_price_usd=live_quote.asset_price_usd
            )
        prices = await self.invoice_service.find_chain_prices(invoice.chain_key)
        if prices is None:
            return None
        asset_price_usd = prices.asset_prices_usd[asset.symbol]
        fiat_price_usd = prices.fiat_price_usd(invoice.fiat_currency or "USD")
        return CreditPrices(asset_price_in_fiat=asset_price_usd / fiat_price_usd, asset_price_usd=asset_price_usd)

    async def find_rise_block_timestamp(self, invoice: Invoice, *, finalized: BlockHead) -> int:
        """The block time of the newest final payment in the rise, else the finalized head's.

        Final rows in chain order fill what was credited before; a row that reaches past it is part of this rise. A
        payment mined while its quote was live is valued at that quote however late it is finalized or credited
        (2026-10-09: valuing at the finalized head's time underpaid a payer who paid in time). A rise no row explains
        (a contract's internal transfer, the unknown sender's share) and a row made before revision 0003 fall back
        to the finalized head's time.
        """
        credited_before = invoice.received_base_units
        running_total = 0
        newest_rise_payment_timestamp: int | None = None
        for payment in await self.repository.list_payments([invoice.id]):
            if not payment.final:
                continue
            running_total += payment.amount_base_units
            if running_total > credited_before:
                newest_rise_payment_timestamp = payment.block_timestamp
        return newest_rise_payment_timestamp if newest_rise_payment_timestamp is not None else finalized.timestamp

    async def credit_finalized_rise(
        self, invoice: Invoice, *, asset: AssetConfiguration, gross_finalized: int, finalized: BlockHead
    ) -> None:
        block_timestamp = await self.find_rise_block_timestamp(invoice, finalized=finalized)
        await self.credit_rise(invoice, asset=asset, gross_received=gross_finalized, block_timestamp=block_timestamp)

    async def credit_rise(
        self, invoice: Invoice, *, asset: AssetConfiguration, gross_received: int, block_timestamp: int
    ) -> None:
        """Credit the rise to ``gross_received``, valued at ``block_timestamp``'s prices, with the status event;
        the Monero credit path (``monero_readings_service.py``) shares it."""
        prices = await self.credit_prices(invoice, asset=asset, block_timestamp=block_timestamp)
        if prices is None:
            # No price to value the credit at: it is credited on a later tick, nothing is lost.
            return
        previous_status = invoice.status
        state = apply_credit(
            accounting_state_of(invoice),
            gross_received_base_units=gross_received,
            terms=terms_of(
                invoice, asset=asset, minimum_refund_usd=self.invoice_service.wiring.invoice_settings.minimum_refund_usd
            ),
            prices=prices,
        )
        invoice.received_base_units = state.received_base_units
        invoice.credited_fiat_amount = state.credited_fiat_amount
        invoice.excess_base_units = state.excess_base_units
        invoice.status = state.status
        if state.status.is_settled_by_payment and invoice.paid_at is None:
            invoice.paid_at = datetime.now(UTC)
        if state.status != previous_status or state.status == InvoiceStatus.UNDERPAID:
            await self.invoice_service.record_invoice_event(invoice, event_type=STATUS_EVENTS[state.status])

    # The clock

    async def expire_and_close_invoices(self, chain_key: str) -> None:
        """Expire invoices with nothing final (a confirming one waits for finality first) and close underpaid ones
        whose ``underpaid_open_until`` passed, sweeping what they hold."""
        now = datetime.now(UTC)
        due = [
            invoice
            for invoice in await self.repository.list_unclosed_invoices(chain_key)
            if (
                invoice.status in {InvoiceStatus.AWAITING_ASSET, InvoiceStatus.AWAITING_PAYMENT}
                and invoice.expires_at is not None
                and invoice.expires_at <= now
            )
            or (
                invoice.status == InvoiceStatus.UNDERPAID
                and invoice.underpaid_open_until is not None
                and invoice.underpaid_open_until <= now
            )
        ]
        for invoice in await self.repository.lock_invoices([invoice.id for invoice in due]):
            # Selection can change the chain and renew the deadline while this watcher waits for the lock.
            # A payment or account action can change the status too. Judge the refreshed, locked row.
            now = datetime.now(UTC)
            awaiting_expiry = (
                invoice.status in {InvoiceStatus.AWAITING_ASSET, InvoiceStatus.AWAITING_PAYMENT}
                and invoice.expires_at is not None
                and invoice.expires_at <= now
            )
            underpaid_closure = (
                invoice.status == InvoiceStatus.UNDERPAID
                and invoice.underpaid_open_until is not None
                and invoice.underpaid_open_until <= now
            )
            if (
                invoice.closed_at is not None
                or invoice.chain_key != chain_key
                or not (awaiting_expiry or underpaid_closure)
            ):
                continue
            invoice.closed_at = now
            if invoice.status == InvoiceStatus.UNDERPAID:
                asset = self.chains_service.get_asset(invoice.chain_key, symbol=invoice.asset_symbol or "")
                await self.invoice_service.request_sweep_of_a_closing_deposit(invoice, asset=asset)
                continue
            await self.invoice_service.change_status(
                invoice, status=InvoiceStatus.EXPIRED, event_type=InvoiceEventType.EXPIRED
            )

    # The rescan, one slice of deposits per call (PLAN.md section 4: late and stray money)

    async def apply_rescan(self, *, rescanned: list[RescannedDeposit], finalized_block_number: int) -> None:
        """Money on a closed invoice is swept; it is late (``late_payment`` and its event) unless the invoice already
        counted it: its own asset's credited payments, or stray money the watcher recorded while it was live (INC-001:
        the payer and the account were told then). Money in another asset on a live invoice is the watcher's, every
        tick (stray payments), not the rescan's."""
        with_money = [deposit for deposit in rescanned if any(deposit.balances_by_symbol.values())]
        balances_by_invoice = {deposit.invoice_id: deposit.balances_by_symbol for deposit in with_money}
        for invoice in await self.repository.lock_invoices(sorted(balances_by_invoice)):
            if invoice.closed_at is None or invoice.sweep_blocked:
                continue
            chain = self.chains_service.get_chain(invoice.chain_key)
            # Dust is not a late payment and not worth a sweep's gas (anyone can send 1 wei to an old deposit).
            held = {
                symbol: balance
                for symbol, balance in balances_by_invoice[invoice.id].items()
                if (asset := chain.find_asset(symbol)) is not None and balance >= asset.minimum_sweep_base_units
            }
            # A sweep under way (a closing underpaid deposit's, say) is about to move what is held.
            if not held or await self.invoice_service.settlement_service.has_active_sweep(invoice.id):
                continue
            await self.invoice_service.request_sweep(invoice)
            late = await self.find_late_balances(
                invoice, held_by_symbol=held, finalized_block_number=finalized_block_number
            )
            if not late:
                continue
            invoice.late_payment = True
            for symbol, late_base_units in late.items():
                log.emit(
                    logger,
                    event=LatePaymentFound(
                        invoice_id=invoice.id, asset_symbol=symbol, late_base_units=str(late_base_units)
                    ),
                )
            await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.LATE_PAYMENT)

    async def find_late_balances(
        self, invoice: Invoice, *, held_by_symbol: dict[str, int], finalized_block_number: int
    ) -> dict[str, int]:
        """The part of each held balance the invoice did not already count. All that ever reached the deposit in an
        asset is its balance plus its finalized sweeps; the invoice counted ``received_base_units`` of its own asset
        (credited while it was live) and, in another asset, what its stray rows add up to. Only money past that is
        late, at most the balance.
        Credited money can still sit in a closed deposit: a lapsed underpaid invoice whose closing sweep reverted on
        a frozen token, then ``sweep-invoice --skip-token`` moved the rest and cleared ``sweep_blocked`` (2026-10-13:
        each such invoice got one false ``late_payment``). A real late payment raises the total past what was
        counted, so it is still flagged. A late part under the asset's minimum sweep is dust, not a late payment.
        Sweeps count up to ``finalized_block_number``, the block the balances were read at: one mined after it is
        not out of that balance yet (a tick on a lagging endpoint would count it twice)."""
        chain = self.chains_service.get_chain(invoice.chain_key)
        stray_by_symbol: dict[str, int] = {}
        for stray_payment in await self.repository.list_stray_payments([invoice.id]):
            stray_by_symbol[stray_payment.asset_symbol] = (
                stray_by_symbol.get(stray_payment.asset_symbol, 0) + stray_payment.amount_base_units
            )
        swept_by_token: dict[str | None, int] = {}
        for event in await self.repository.list_sweep_events(
            invoice.chain_key, deposit_addresses=[require_deposit_address(invoice)]
        ):
            if event.block_number <= finalized_block_number:
                swept_by_token[event.token_address] = (
                    swept_by_token.get(event.token_address, 0) + event.amount_base_units
                )
        late: dict[str, int] = {}
        for symbol, balance in held_by_symbol.items():
            asset = chain.find_asset(symbol)
            if asset is None:
                late[symbol] = balance
                continue
            counted_by_invoice = (
                invoice.received_base_units if symbol == invoice.asset_symbol else stray_by_symbol.get(symbol, 0)
            )
            ever_reached_deposit = balance + swept_by_token.get(asset.token_address, 0)
            late_base_units = min(balance, ever_reached_deposit - counted_by_invoice)
            if late_base_units >= asset.minimum_sweep_base_units:
                late[symbol] = late_base_units
        return late

    # Automatic refunds that waited for a price (PLAN.md section 6, 2026-10-12)

    def defer_automatic_refunds(self, invoice: Invoice, *, asset_symbols: set[str]) -> None:
        """An automatic refund (overpay excess or stray money) that cannot be decided for want of a price is never
        taken as "below the minimum": the invoice is marked and ``redecide_deferred_automatic_refunds`` decides it
        again every tick until a price is readable (after a stray refund silently never fired)."""
        if not asset_symbols or invoice.automatic_refund_deferred_since is not None:
            return
        invoice.automatic_refund_deferred_since = datetime.now(UTC)
        log.emit(
            logger,
            event=AutomaticRefundDeferred(invoice_id=invoice.id, asset_symbols=",".join(sorted(asset_symbols))),
        )

    async def redecide_deferred_automatic_refunds(self, chain_key: str) -> None:
        """Every marked invoice's automatic refunds decided again on the locked row, closed or not: one indexed query,
        and one price read that skips the rest while no feed answers. Each decision is idempotent, as the refundable
        amounts subtract every refund that moved or may move, so a refund recorded before is never recorded twice.
        The mark stays while a decision still waits for a price."""
        invoice_ids = await self.repository.list_invoice_ids_with_deferred_automatic_refunds(chain_key)
        if not invoice_ids or await self.invoice_service.find_chain_prices(chain_key) is None:
            return
        for invoice in await self.repository.lock_invoices(invoice_ids):
            deferred_since = invoice.automatic_refund_deferred_since
            if deferred_since is None:
                continue
            overpay_deferred = await self.invoice_service.refund_overpaid_excess_automatically(invoice)
            stray_deferred = await self.invoice_service.refund_stray_payments_automatically(invoice)
            if overpay_deferred or stray_deferred:
                continue
            invoice.automatic_refund_deferred_since = None
            deferred_seconds = int((datetime.now(UTC) - deferred_since).total_seconds())
            log.emit(
                logger,
                event=AutomaticRefundDecidedAfterDeferral(invoice_id=invoice.id, deferred_seconds=deferred_seconds),
            )

    # Settlement outcomes (PLAN.md sections 4 and 6)

    async def apply_settlement_outcomes(self, outcomes: list[SettlementOutcome]) -> None:
        """Run on the session that resolved the operations, before it commits (the watcher's ``SettlementRunner``
        and the owner's CLI), so an outcome and its effect land together or not at all (2026-10-12)."""
        operations = {outcome.operation.invoice_id: [] for outcome in outcomes}
        for outcome in outcomes:
            operations[outcome.operation.invoice_id].append(outcome.operation)
        for invoice in await self.repository.lock_invoices(sorted(operations)):
            for operation in operations[invoice.id]:
                match (operation.kind, operation.status):
                    case (OperationKind.SWEEP, OperationStatus.FAILED) if operation.is_sweep_for_another_key:
                        # Its key was frozen: a new sweep for another key, in this transaction. Left to the next
                        # reading, a closed invoice's would wait for the rescan's pass (2026-10-13). It leaves out
                        # what the ended one did (``sweep-invoice --skip-token``).
                        await self.invoice_service.request_sweep(
                            invoice, skipped_token_addresses=operation.skipped_token_addresses
                        )
                    case (OperationKind.SWEEP, OperationStatus.FAILED):
                        invoice.sweep_blocked = True
                        await self.invoice_service.record_invoice_event(
                            invoice, event_type=InvoiceEventType.SWEEP_FAILED
                        )
                    case (OperationKind.REFUND, OperationStatus.WAITING_FOR_FLOAT):
                        await self.invoice_service.record_invoice_event(
                            invoice, event_type=InvoiceEventType.REFUND_WAITING_FOR_FLOAT
                        )
                    case (OperationKind.REFUND, OperationStatus.NEEDS_OWNER):
                        await self.invoice_service.record_invoice_event(
                            invoice, event_type=InvoiceEventType.REFUND_NEEDS_OWNER
                        )
                    case (OperationKind.REFUND, OperationStatus.FINAL):
                        await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.REFUNDED)
                        await self.cancel_if_refunded_in_full(invoice)
                    case _:
                        pass

    async def settle_refund_needing_owner(
        self, operation_id: int, *, paid_transaction_hash: str | None, nonce_taking_transaction_hash: str | None
    ) -> OperationValue:
        """The CLI's ``refunds mark-paid`` (with the transaction that paid it) and ``refunds mark-unpaid`` (no paying
        transaction; the one that took the refund's nonce, unless it never used one): settlements checks the chain,
        then the outcome is applied as the watcher applies one (PLAN.md section 6)."""
        settlement_service = self.invoice_service.settlement_service
        rpc_clients = self.invoice_service.wiring.rpc_clients
        if paid_transaction_hash is None:
            outcome = await settlement_service.mark_refund_unpaid_by_owner(
                operation_id, nonce_taking_transaction_hash=nonce_taking_transaction_hash, rpc_clients=rpc_clients
            )
        else:
            outcome = await settlement_service.mark_refund_paid_by_owner(
                operation_id, transaction_hash=paid_transaction_hash, rpc_clients=rpc_clients
            )
        await self.apply_settlement_outcomes([outcome])
        return outcome.operation

    async def cancel_if_refunded_in_full(self, invoice: Invoice) -> None:
        """An underpaid invoice whose whole received amount went back is cancelled (PLAN.md section 4)."""
        if invoice.status != InvoiceStatus.UNDERPAID:
            return
        # Only refunds in the invoice's own asset: a stray coin sent back is another asset's base units (INC-001).
        refunded = sum(
            operation.moved_base_units or 0
            for operation in await self.invoice_service.settlement_service.list_operations_for_invoices([invoice.id])
            if operation.kind == OperationKind.REFUND
            and operation.status == OperationStatus.FINAL
            and operation.asset_symbol == invoice.asset_symbol
        )
        if refunded >= invoice.received_base_units:
            await self.invoice_service.change_status(
                invoice, status=InvoiceStatus.CANCELLED, event_type=InvoiceEventType.CANCELLED
            )
