"""The request-scoped StaticDepositAddressService, on the request's one session (the invoice service's)."""

from typing import Annotated

from fastapi import Depends

from app.lifespan.dependencies import DatabaseSession
from app.payments.invoices.dependencies import InvoiceServiceDependency
from app.payments.static_deposit_addresses.service import StaticDepositAddressService


def get_static_deposit_address_service(
    session: DatabaseSession, *, invoice_service: InvoiceServiceDependency
) -> StaticDepositAddressService:
    return StaticDepositAddressService.on_session(session, invoice_service=invoice_service)


StaticDepositAddressServiceDependency = Annotated[
    StaticDepositAddressService, Depends(get_static_deposit_address_service)
]
