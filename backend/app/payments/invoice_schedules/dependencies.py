"""The request-scoped InvoiceScheduleService, on the request's one session (the invoice service's)."""

from typing import Annotated

from fastapi import Depends

from app.lifespan.dependencies import DatabaseSession
from app.payments.invoice_schedules.service import InvoiceScheduleService
from app.payments.invoices.dependencies import InvoiceServiceDependency


def get_invoice_schedule_service(
    session: DatabaseSession, *, invoice_service: InvoiceServiceDependency
) -> InvoiceScheduleService:
    return InvoiceScheduleService.on_session(session, invoice_service=invoice_service)


InvoiceScheduleServiceDependency = Annotated[InvoiceScheduleService, Depends(get_invoice_schedule_service)]
