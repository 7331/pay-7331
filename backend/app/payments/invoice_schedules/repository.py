"""Queries for invoice_schedules; no decisions."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.invoice_schedules.enums import InvoiceScheduleStatus
from app.payments.invoice_schedules.models import InvoiceSchedule

LIVE_STATUSES = [InvoiceScheduleStatus.ACTIVE.value, InvoiceScheduleStatus.PAUSED.value]


class InvoiceScheduleRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def insert_invoice_schedule(self, invoice_schedule: InvoiceSchedule) -> InvoiceSchedule:
        self.session.add(invoice_schedule)
        await self.session.flush()
        await self.session.refresh(invoice_schedule)
        return invoice_schedule

    async def save_invoice_schedule(self, invoice_schedule: InvoiceSchedule) -> InvoiceSchedule:
        """Writes the changes and reloads the row, so the database-set ``updated_at`` can be read without IO."""
        await self.session.flush()
        await self.session.refresh(invoice_schedule)
        return invoice_schedule

    async def find_invoice_schedule(self, invoice_schedule_id: str, *, lock: bool = False) -> InvoiceSchedule | None:
        """``lock`` takes the row ``FOR UPDATE`` until the transaction ends: the account's changes and the watcher's
        invoice for a period never interleave on one schedule."""
        statement = select(InvoiceSchedule).where(InvoiceSchedule.id == invoice_schedule_id)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self.session.scalar(statement)

    async def lock_due_invoice_schedule(self, invoice_schedule_id: str, *, now: datetime) -> InvoiceSchedule | None:
        """The schedule, locked, if it is still active and due; None when another runner holds it (``SKIP LOCKED``) or
        it was billed, paused or ended since it was listed."""
        return await self.session.scalar(
            select(InvoiceSchedule)
            .where(
                InvoiceSchedule.id == invoice_schedule_id,
                InvoiceSchedule.status == InvoiceScheduleStatus.ACTIVE.value,
                InvoiceSchedule.account_id.is_not(None),
                InvoiceSchedule.next_attempt_at <= now,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )

    async def list_due_invoice_schedule_ids(self, *, now: datetime, limit: int) -> list[str]:
        """Active schedules of an account whose next attempt is due, the longest waiting first
        (``ix_invoice_schedules_next_attempt_at_active``)."""
        return list(
            await self.session.scalars(
                select(InvoiceSchedule.id)
                .where(
                    InvoiceSchedule.status == InvoiceScheduleStatus.ACTIVE.value,
                    InvoiceSchedule.next_attempt_at <= now,
                    InvoiceSchedule.account_id.is_not(None),
                )
                .order_by(InvoiceSchedule.next_attempt_at, InvoiceSchedule.id)
                .limit(limit)
            )
        )

    async def list_account_invoice_schedules(self, account_id: int, *, limit: int) -> list[InvoiceSchedule]:
        """The account's schedules, active and paused ones first (the cap counts only those, so ``limit`` at the cap
        still lists every live one), then newest first."""
        return list(
            await self.session.scalars(
                select(InvoiceSchedule)
                .where(InvoiceSchedule.account_id == account_id)
                .order_by(
                    InvoiceSchedule.status.in_(LIVE_STATUSES).desc(),
                    InvoiceSchedule.created_at.desc(),
                    InvoiceSchedule.id.desc(),
                )
                .limit(limit)
            )
        )

    async def count_live_account_invoice_schedules(self, account_id: int) -> int:
        """Schedules of the account that have not ended (active or paused)."""
        return (
            await self.session.scalar(
                select(func.count()).where(
                    InvoiceSchedule.account_id == account_id, InvoiceSchedule.status.in_(LIVE_STATUSES)
                )
            )
        ) or 0

    async def live_account_invoice_schedule_uses_prefix(self, account_id: int, *, order_id_prefix: str) -> bool:
        return (
            await self.session.scalar(
                select(InvoiceSchedule.id)
                .where(
                    InvoiceSchedule.account_id == account_id,
                    InvoiceSchedule.status.in_(LIVE_STATUSES),
                    InvoiceSchedule.order_id_prefix == order_id_prefix,
                )
                .limit(1)
            )
        ) is not None

    async def lock_account_invoice_schedule_creation(self, account_id: int) -> None:
        """Held until the transaction ends, so two creations at once cannot both pass the cap or both take a prefix."""
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"invoice_schedule_creation:{account_id}", 0)))
        )
