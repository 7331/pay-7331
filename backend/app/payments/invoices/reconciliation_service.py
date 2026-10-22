"""InvoiceReconciliationService: the invoice reads of the operator's ``reconcile`` and the watcher's operator alerts
(2026-10-12). Read only: it never locks or changes an invoice."""

from datetime import datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.invoices.enums import InvoiceStatus
from app.payments.invoices.models import Invoice, require_deposit_address
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.types import (
    DeferredAutomaticRefund,
    DepositOnChain,
    InvoiceSettlementState,
    SenderLessStrayPayment,
)

# No payment is due any more: whatever the deposit holds should be swept (or is dust).
PAYMENT_WINDOW_OVER_STATUSES = frozenset(
    {InvoiceStatus.PAID, InvoiceStatus.OVERPAID, InvoiceStatus.EXPIRED, InvoiceStatus.CANCELLED}
)


def settlement_state_of(invoice: Invoice) -> InvoiceSettlementState:
    return InvoiceSettlementState(
        invoice_id=invoice.id,
        status=invoice.status.value,
        payment_window_over=invoice.closed_at is not None or invoice.status in PAYMENT_WINDOW_OVER_STATUSES,
        sweep_blocked=invoice.sweep_blocked,
        deposit_address=require_deposit_address(invoice),
        asset_symbol=invoice.asset_symbol,
        received_base_units=invoice.received_base_units,
    )


class InvoiceReconciliationService:
    def __init__(self, *, repository: InvoiceRepository) -> None:
        self.repository = repository

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(repository=InvoiceRepository(session=session))

    async def list_deposits_after(
        self, chain_key: str, *, after_invoice_id: str | None, limit: int
    ) -> list[DepositOnChain]:
        return [
            DepositOnChain(invoice_id=invoice_id, deposit_address=deposit_address)
            for invoice_id, deposit_address in await self.repository.list_deposits_after(
                chain_key, after_invoice_id=after_invoice_id, limit=limit
            )
        ]

    async def list_settlement_states(self, invoice_ids: list[str]) -> list[InvoiceSettlementState]:
        # An invoice still waiting for its payer's pick of a chain has no deposit to settle (PLAN.md section 15).
        return [
            settlement_state_of(invoice)
            for invoice in await self.repository.list_invoices(invoice_ids)
            if invoice.deposit_address is not None
        ]

    async def list_deposit_sweep_transaction_hashes(
        self, chain_key: str, *, deposit_addresses: list[str]
    ) -> dict[str, set[str]]:
        """Deposit address to the transactions of its recorded finalized ``DepositSwept`` events."""
        transaction_hashes_by_deposit: dict[str, set[str]] = {}
        if not deposit_addresses:
            return transaction_hashes_by_deposit
        for deposit_address, transaction_hash in await self.repository.list_deposit_sweep_transaction_hashes(
            chain_key, deposit_addresses=deposit_addresses
        ):
            transaction_hashes_by_deposit.setdefault(deposit_address, set()).add(transaction_hash)
        return transaction_hashes_by_deposit

    async def list_sweep_blocked_invoices(self, chain_key: str) -> list[InvoiceSettlementState]:
        """A sweep failed and nothing has swept the deposit since (`sweep-invoice` clears it)."""
        return [
            settlement_state_of(invoice) for invoice in await self.repository.list_sweep_blocked_invoices(chain_key)
        ]

    async def list_sender_less_stray_payments(self, chain_key: str) -> list[SenderLessStrayPayment]:
        return [
            SenderLessStrayPayment(
                stray_payment_id=row.id,
                invoice_id=row.invoice_id,
                asset_symbol=row.asset_symbol,
                amount_base_units=row.amount_base_units,
                block_number=row.block_number,
                final=row.final,
            )
            for row in await self.repository.list_sender_less_stray_payments(chain_key)
        ]

    async def list_automatic_refunds_deferred_before(
        self, chain_key: str, *, deferred_before: datetime
    ) -> list[DeferredAutomaticRefund]:
        return [
            DeferredAutomaticRefund(invoice_id=invoice.id, deferred_since=invoice.automatic_refund_deferred_since)
            for invoice in await self.repository.list_invoices_deferred_before(
                chain_key, deferred_before=deferred_before
            )
            if invoice.automatic_refund_deferred_since is not None
        ]
