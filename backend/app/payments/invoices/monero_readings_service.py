"""MoneroReadingsService: what the scanner sidecar wrote to ``monero_payments``, applied to XMR invoices (PLAN.md
section 14), on the caller's session and transaction.

The section 4 state machine is unchanged; Monero readings map onto it (rules in ``monero_payment_rules.py``):

- A pool transaction paying the subaddress makes the invoice "seen" (``invoice.seen``) and changes nothing else.
- An output in a block whose first check on a second operator agreed is credited (``credited_at``); credited outputs
  play `latest`: a rise drives ``awaiting_payment`` to ``confirming`` (``invoice.payment_detected``).
- Gross received is the sum of credited outputs with ``confirmations_final`` confirmations at the confirmed tip (the
  lower of the sidecar's tip and a second operator's) whose second check agreed; a rise is credited as a finalized
  rise is on EVM, valued at the quote live when its newest output was first seen (a block row's ``first_seen_at`` is
  its block's time), with the same paid, underpaid and overpaid decisions. There is no sweep and no refund on this kind: a paid or
  overpaid invoice closes at once (the money is in the receiver's wallet), an overpaid one's excess is the receiver's
  to settle (its ``refund_offer`` says ``receiver_settles``), an underpaid one stays open for top-ups.
- A check that disagreed holds the invoice at ``confirming`` and credits nothing, from that row or any other; the
  account gets ``invoice.payment_held`` once and the watcher tells the operator.
- A credited output that is no longer creditable is uncredited: its block left the chain (the sidecar nulls height
  and hash and resets the checks), even when the transaction was mined again before this tick looked; it is
  credited again once its new block's check agrees. An invoice whose credited outputs all went falls back to
  ``awaiting_payment``.
- An output that reaches an invoice closed within the chain's ``recent_deposit_window_seconds`` is a late payment:
  credited as a row, ``late_payment`` set and ``invoice.late_payment`` recorded, as on EVM; no amount or status
  changes and there is no sweep. A late row whose block then left the chain is uncredited in the same pass; the
  invoice keeps ``late_payment``, as an EVM invoice keeps it once set.
- The invoice's ``latest_reading_block_number`` is the confirmed tip (the tip final counting reads), so the payer
  page's "n of 10" and "I paid" count from the same tip as the watcher.

Invoices are locked in id order, then their payment rows, as the sidecar only ever updates a row on its own.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Self

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.chains.types import AssetConfiguration, MoneroChainConfiguration
from app.payments.invoices.chain_readings_service import ChainReadingsService
from app.payments.invoices.enums import InvoiceEventType, InvoiceStatus
from app.payments.invoices.log_events import (
    MoneroLatePaymentCredited,
    MoneroPaymentCredited,
    MoneroPaymentHeld,
    MoneroPaymentUncredited,
)
from app.payments.invoices.models import Invoice, MoneroPayment
from app.payments.invoices.monero_payment_rules import (
    invoice_confirmations_final,
    is_counted_at_final,
    is_creditable,
    is_credited,
    is_held,
    is_seen_in_pool,
    is_waiting_for_final,
)
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.wiring import InvoiceWiring
from app.shared import logging as log

logger = log.get_logger(__name__)


class MoneroReadingsService:
    def __init__(self, *, repository: InvoiceRepository, chain_readings_service: ChainReadingsService) -> None:
        self.repository = repository
        self.chain_readings_service = chain_readings_service
        self.invoice_service = chain_readings_service.invoice_service

    @classmethod
    def on_session(cls, session: AsyncSession, *, redis: Redis, wiring: InvoiceWiring) -> Self:
        return cls(
            repository=InvoiceRepository(session=session),
            chain_readings_service=ChainReadingsService.on_session(session, redis=redis, wiring=wiring),
        )

    async def apply_monero_payments(
        self, chain: MoneroChainConfiguration, *, confirmed_tip_height: int | None
    ) -> list[str]:
        """One pass over the open invoices of the chain that have payment rows: credits from the rows' cross-checks,
        final counting at the confirmed tip (``None``: nothing counts as final this tick, and the invoices keep the
        tip they last stored). Returns the ids of those held by a disagreeing check, for the operator alert."""
        open_invoice_ids = [invoice.id for invoice in await self.repository.list_unclosed_invoices(chain.key)]
        invoice_ids_with_rows = sorted(
            {
                monero_payment.invoice_id
                for monero_payment in await self.repository.list_monero_payments(open_invoice_ids)
            }
        )
        locked_invoices = await self.repository.lock_invoices(invoice_ids_with_rows)
        rows_by_invoice: dict[str, list[MoneroPayment]] = defaultdict(list)
        for monero_payment in await self.repository.list_monero_payments(invoice_ids_with_rows, lock=True):
            rows_by_invoice[monero_payment.invoice_id].append(monero_payment)
        asset = chain.assets[0]
        held_invoice_ids: list[str] = []
        for invoice in locked_invoices:
            if invoice.closed_at is not None or invoice.asset_symbol is None:
                continue
            invoice_rows = rows_by_invoice[invoice.id]
            if confirmed_tip_height is not None:
                invoice.latest_reading_block_number = confirmed_tip_height
            self.credit_and_uncredit_rows(invoice, invoice_rows=invoice_rows)
            held = any(is_held(monero_payment) for monero_payment in invoice_rows)
            await self.apply_invoice_reading(
                invoice,
                chain=chain,
                asset=asset,
                invoice_rows=invoice_rows,
                confirmed_tip_height=confirmed_tip_height,
                held=held,
            )
            if held:
                held_invoice_ids.append(invoice.id)
        return held_invoice_ids

    def credit_and_uncredit_rows(self, invoice: Invoice, *, invoice_rows: list[MoneroPayment]) -> None:
        now = datetime.now(UTC)
        for monero_payment in invoice_rows:
            if monero_payment.credited_at is None and is_creditable(monero_payment):
                monero_payment.credited_at = now
                log.emit(
                    logger, event=MoneroPaymentCredited(invoice_id=invoice.id, monero_payment_id=monero_payment.id)
                )
            elif monero_payment.credited_at is not None and not is_creditable(monero_payment):
                monero_payment.credited_at = None
                log.emit(
                    logger, event=MoneroPaymentUncredited(invoice_id=invoice.id, monero_payment_id=monero_payment.id)
                )

    async def apply_invoice_reading(
        self,
        invoice: Invoice,
        *,
        chain: MoneroChainConfiguration,
        asset: AssetConfiguration,
        invoice_rows: list[MoneroPayment],
        confirmed_tip_height: int | None,
        held: bool,
    ) -> None:
        """``ChainReadingsService.apply_deposit_reading``'s steps with credited outputs as `latest` and those counted
        at final (at the confirmed tip) as the finalized gross; nothing is swept, a settled invoice closes. A held
        invoice credits no rise at all (PLAN.md section 14: nothing credits until the operator settles it)."""
        credited_rows = [monero_payment for monero_payment in invoice_rows if is_credited(monero_payment)]
        final_rows = [
            monero_payment
            for monero_payment in credited_rows
            if is_counted_at_final(
                monero_payment,
                tip_height=confirmed_tip_height,
                confirmations_final=invoice_confirmations_final(invoice, chain=chain),
            )
        ]
        gross_latest = sum(monero_payment.amount_piconero for monero_payment in credited_rows)
        gross_final = sum(monero_payment.amount_piconero for monero_payment in final_rows)
        if gross_final > invoice.received_base_units and not held:
            await self.chain_readings_service.credit_rise(
                invoice,
                asset=asset,
                gross_received=gross_final,
                block_timestamp=self.find_rise_first_seen_timestamp(invoice, final_rows=final_rows),
            )
        if gross_latest > max(invoice.latest_gross_base_units, invoice.received_base_units):
            if invoice.status == InvoiceStatus.AWAITING_PAYMENT:
                invoice.status = InvoiceStatus.CONFIRMING
            await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.PAYMENT_DETECTED)
        elif invoice.status == InvoiceStatus.CONFIRMING and gross_latest <= invoice.received_base_units and not held:
            # What was credited left the chain (a reorg before final): nothing was received after all.
            invoice.status = InvoiceStatus.AWAITING_PAYMENT
        if held and invoice.status == InvoiceStatus.AWAITING_PAYMENT:
            invoice.status = InvoiceStatus.CONFIRMING
        invoice.latest_gross_base_units = max(gross_latest, invoice.received_base_units)
        if invoice.status.is_settled_by_payment:
            # The payer paid the receiver's own wallet: nothing to sweep, so a settled invoice is done.
            invoice.closed_at = datetime.now(UTC)
        await self.record_seen_event(invoice, invoice_rows=invoice_rows)
        if held:
            await self.record_held_event(invoice)

    def find_rise_first_seen_timestamp(self, invoice: Invoice, *, final_rows: list[MoneroPayment]) -> int:
        """When the newest output of the rise was first seen (in the pool, usually), as Unix seconds: a payer who paid
        while the quote was live keeps it however long the confirmations take, as on EVM; outputs in chain order fill
        what was credited before."""
        running_total = 0
        rise_first_seen_times: list[datetime] = []
        for monero_payment in final_rows:
            running_total += monero_payment.amount_piconero
            if running_total > invoice.received_base_units:
                rise_first_seen_times.append(monero_payment.first_seen_at)
        return int(max(rise_first_seen_times).timestamp())

    async def record_seen_event(self, invoice: Invoice, *, invoice_rows: list[MoneroPayment]) -> None:
        """``invoice.seen`` while a pool transaction pays it, at most once per transaction the invoice has had."""
        if not any(is_seen_in_pool(monero_payment) for monero_payment in invoice_rows):
            return
        transaction_count = len(
            {monero_payment.txid for monero_payment in invoice_rows if not monero_payment.double_spend_seen}
        )
        seen_event_count = (
            await self.invoice_service.account_event_service.count_invoice_events(
                [invoice.id], event_types=[InvoiceEventType.SEEN.value]
            )
        ).get((invoice.id, InvoiceEventType.SEEN.value), 0)
        if seen_event_count < transaction_count:
            await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.SEEN)

    async def record_held_event(self, invoice: Invoice) -> None:
        held_event_count = (
            await self.invoice_service.account_event_service.count_invoice_events(
                [invoice.id], event_types=[InvoiceEventType.PAYMENT_HELD.value]
            )
        ).get((invoice.id, InvoiceEventType.PAYMENT_HELD.value), 0)
        if held_event_count == 0:
            log.emit(logger, event=MoneroPaymentHeld(invoice_id=invoice.id))
            await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.PAYMENT_HELD)

    # Late payments

    async def apply_late_monero_payments(self, chain: MoneroChainConfiguration) -> None:
        """Outputs that reached an invoice closed within ``recent_deposit_window_seconds`` (the sidecar keeps watching
        its wallet that long): each creditable one is credited as a row and the invoice marked late, with
        ``invoice.late_payment`` once per tick that finds new ones; a credited row of a closed invoice that is no
        longer creditable (its block left the chain) is uncredited. Nothing else changes: the money is already in the
        receiver's wallet, so there is nothing to sweep, the closed invoice's amounts stand, and ``late_payment``
        stays set once set, as on EVM."""
        closed_after = datetime.now(UTC) - timedelta(seconds=chain.recent_deposit_window_seconds)
        late_invoice_ids = await self.repository.list_recently_closed_invoice_ids_with_monero_payments_to_decide(
            chain.key, closed_after=closed_after
        )
        locked_invoices = await self.repository.lock_invoices(late_invoice_ids)
        rows_by_invoice: dict[str, list[MoneroPayment]] = defaultdict(list)
        for monero_payment in await self.repository.list_monero_payments(late_invoice_ids, lock=True):
            rows_by_invoice[monero_payment.invoice_id].append(monero_payment)
        now = datetime.now(UTC)
        for invoice in locked_invoices:
            if invoice.closed_at is None:
                continue
            for monero_payment in rows_by_invoice[invoice.id]:
                if monero_payment.credited_at is not None and not is_creditable(monero_payment):
                    monero_payment.credited_at = None
                    log.emit(
                        logger,
                        event=MoneroPaymentUncredited(invoice_id=invoice.id, monero_payment_id=monero_payment.id),
                    )
            late_rows = [
                monero_payment
                for monero_payment in rows_by_invoice[invoice.id]
                if monero_payment.credited_at is None and is_creditable(monero_payment)
            ]
            if not late_rows:
                continue
            for monero_payment in late_rows:
                monero_payment.credited_at = now
                log.emit(
                    logger,
                    event=MoneroLatePaymentCredited(invoice_id=invoice.id, monero_payment_id=monero_payment.id),
                )
            invoice.late_payment = True
            await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.LATE_PAYMENT)

    # The clock

    async def expire_and_close_monero_invoices(
        self, chain: MoneroChainConfiguration, *, confirmed_tip_height: int | None
    ) -> None:
        """As ``ChainReadingsService.expire_and_close_invoices``, with two differences: an invoice waits while an
        output of it is in the pool or just left it (``monero_payment_rules.has_just_left_pool``), or in a block and
        not yet counted at final (its payer paid in time; a Monero
        block takes about two minutes and its check a few seconds more), an underpaid one too, so closing it never
        drops a top-up in flight; and closing an underpaid one sweeps nothing."""
        now = datetime.now(UTC)
        due_invoices = [
            invoice
            for invoice in await self.repository.list_unclosed_invoices(chain.key)
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
        due_invoice_ids = [invoice.id for invoice in due_invoices]
        locked_invoices = await self.repository.lock_invoices(due_invoice_ids)
        # Each invoice expires only when its own threshold says its outputs are not on their way.
        confirmations_final_by_invoice = {
            invoice.id: invoice_confirmations_final(invoice, chain=chain) for invoice in locked_invoices
        }
        waiting_invoice_ids = {
            monero_payment.invoice_id
            for monero_payment in await self.repository.list_monero_payments(due_invoice_ids)
            if monero_payment.invoice_id in confirmations_final_by_invoice
            and is_waiting_for_final(
                monero_payment,
                tip_height=confirmed_tip_height,
                confirmations_final=confirmations_final_by_invoice[monero_payment.invoice_id],
                now=now,
            )
        }
        for invoice in locked_invoices:
            if invoice.closed_at is not None:
                continue
            if invoice.id in waiting_invoice_ids:
                continue
            invoice.closed_at = now
            if invoice.status == InvoiceStatus.UNDERPAID:
                continue
            await self.invoice_service.change_status(
                invoice, status=InvoiceStatus.EXPIRED, event_type=InvoiceEventType.EXPIRED
            )
