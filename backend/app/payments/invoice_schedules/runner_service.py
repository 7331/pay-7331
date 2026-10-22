"""ScheduledInvoiceRunner: the watcher's loop that makes each invoice schedule's due invoice (PLAN.md section 18).

Every ``SCHEDULED_INVOICE_PASS_INTERVAL_SECONDS`` a pass lists at most ``SCHEDULES_PER_PASS`` due schedules (one
indexed read) and bills each in a transaction of its own, so one schedule's refusal (no price, the chain unreachable,
the account at its open-invoice cap, its webhook removed) rolls back only that schedule and is recorded on it with a
backoff, and never holds the others up. A pass that found more due schedules than it took asks for the next pass at
once (``run_process_loop``), so no pass does unbounded work. Two runners at once (a second
watcher, a restart overlapping) are safe: each schedule is billed under its row lock with ``SKIP LOCKED``, and the
invoices' unique ``(invoice_schedule_id, invoice_schedule_period_start)`` index refuses a second invoice for a period.
"""

from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.payments.invoice_schedules.log_events import (
    ScheduledInvoiceFailed,
    ScheduledInvoiceFailedUnexpectedly,
    ScheduledInvoicePeriodAlreadyBilled,
)
from app.payments.invoice_schedules.service import InvoiceScheduleService
from app.payments.invoices.service import InvoiceService
from app.payments.invoices.wiring import InvoiceWiring
from app.shared import logging as log
from app.shared.errors import AppError

logger = log.get_logger(__name__)

SCHEDULED_INVOICE_PASS_INTERVAL_SECONDS = 15
SCHEDULES_PER_PASS = 20
# Seconds until the next attempt after 1, 2, ... failures in a row; the last repeats. A period whose retries run past
# the next period's start is skipped for that one.
RETRY_IN_SECONDS_BY_FAILURES = [60, 120, 300, 900, 1800, 3600]
UNEXPECTED_FAILURE_CODE = "internal_error"
PERIOD_ALREADY_BILLED_FAILURE_CODE = "period_already_billed"
SCHEDULED_INVOICE_UNIQUE_INDEX = "ux_invoices_invoice_schedule_id_period_start"


class ScheduledInvoiceRunner:
    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession], redis: Redis, wiring: InvoiceWiring
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.wiring = wiring

    def build_invoice_schedule_service(self, session: AsyncSession) -> InvoiceScheduleService:
        return InvoiceScheduleService.on_session(
            session, invoice_service=InvoiceService.on_session(session, redis=self.redis, wiring=self.wiring)
        )

    async def create_due_scheduled_invoices(self) -> bool:
        """One pass; False when more schedules were due than it took (run the next pass at once)."""
        now = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            due_invoice_schedule_ids = await self.build_invoice_schedule_service(session).list_due_invoice_schedule_ids(
                now=now, limit=SCHEDULES_PER_PASS + 1
            )
        for invoice_schedule_id in due_invoice_schedule_ids[:SCHEDULES_PER_PASS]:
            await self.create_due_scheduled_invoice(invoice_schedule_id)
        return len(due_invoice_schedule_ids) <= SCHEDULES_PER_PASS

    async def create_due_scheduled_invoice(self, invoice_schedule_id: str) -> None:
        now = datetime.now(UTC)
        try:
            async with self.session_factory() as session, session.begin():
                await self.build_invoice_schedule_service(session).create_due_scheduled_invoice(
                    invoice_schedule_id, now=now
                )
        except AppError as refusal:
            await self.record_failure(invoice_schedule_id, failure_code=refusal.code)
        except IntegrityError as conflict:
            if SCHEDULED_INVOICE_UNIQUE_INDEX not in str(conflict.orig):
                log.exception(logger, event=ScheduledInvoiceFailedUnexpectedly(invoice_schedule_id=invoice_schedule_id))
                await self.record_failure(invoice_schedule_id, failure_code=UNEXPECTED_FAILURE_CODE)
                return
            # Another runner billed this period between the check and the insert. Backing off keeps a schedule whose
            # row disagrees with its invoices (moved back by hand) from hitting the index every pass; the next attempt
            # finds the period's invoice and moves on.
            log.emit(logger, event=ScheduledInvoicePeriodAlreadyBilled(invoice_schedule_id=invoice_schedule_id))
            await self.record_failure(invoice_schedule_id, failure_code=PERIOD_ALREADY_BILLED_FAILURE_CODE)
        # Broad on purpose: one schedule's crash (an RPC error that is not an AppError) must not stop the pass.
        except Exception:
            log.exception(logger, event=ScheduledInvoiceFailedUnexpectedly(invoice_schedule_id=invoice_schedule_id))
            await self.record_failure(invoice_schedule_id, failure_code=UNEXPECTED_FAILURE_CODE)

    async def record_failure(self, invoice_schedule_id: str, *, failure_code: str) -> None:
        async with self.session_factory() as session, session.begin():
            recorded = await self.build_invoice_schedule_service(session).record_scheduled_invoice_failure(
                invoice_schedule_id,
                failure_code=failure_code,
                now=datetime.now(UTC),
                retry_in_seconds_by_failures=RETRY_IN_SECONDS_BY_FAILURES,
            )
        if recorded is not None:
            period_index, consecutive_failure_count, retry_in_seconds = recorded
            log.emit(
                logger,
                event=ScheduledInvoiceFailed(
                    invoice_schedule_id=invoice_schedule_id,
                    period_index=period_index,
                    failure_code=failure_code,
                    consecutive_failure_count=consecutive_failure_count,
                    retry_in_seconds=retry_in_seconds,
                ),
            )
