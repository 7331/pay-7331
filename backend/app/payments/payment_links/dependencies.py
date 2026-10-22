"""The request-scoped PaymentLinkService, on the request's one session (the invoice service's)."""

from typing import Annotated

from fastapi import Depends

from app.lifespan.dependencies import DatabaseSession
from app.payments.invoices.dependencies import InvoiceServiceDependency
from app.payments.payment_links.service import PaymentLinkService


def get_payment_link_service(
    session: DatabaseSession, *, invoice_service: InvoiceServiceDependency
) -> PaymentLinkService:
    return PaymentLinkService.on_session(session, invoice_service=invoice_service)


PaymentLinkServiceDependency = Annotated[PaymentLinkService, Depends(get_payment_link_service)]
