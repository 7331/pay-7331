"""The request-scoped InvoiceService, PaymentChecksService and MoneroWalletSettingsService, and the CSV export's
InvoiceExportService, which opens its own short sessions while the response streams."""

from typing import Annotated

from fastapi import Depends

from app.lifespan.dependencies import DatabaseSession, RedisDependency, ResourcesDependency
from app.payments.invoices.invoice_export_service import InvoiceExportService
from app.payments.invoices.monero_wallet_settings_service import MoneroWalletSettingsService
from app.payments.invoices.payment_checks_service import PaymentChecksService
from app.payments.invoices.service import InvoiceService


def get_invoice_service(
    session: DatabaseSession, *, redis: RedisDependency, resources: ResourcesDependency
) -> InvoiceService:
    return InvoiceService.on_session(session, redis=redis, wiring=resources.invoice_wiring)


InvoiceServiceDependency = Annotated[InvoiceService, Depends(get_invoice_service)]


def get_payment_checks_service(
    session: DatabaseSession, *, redis: RedisDependency, resources: ResourcesDependency
) -> PaymentChecksService:
    return PaymentChecksService.on_session(session, redis=redis, wiring=resources.invoice_wiring)


PaymentChecksServiceDependency = Annotated[PaymentChecksService, Depends(get_payment_checks_service)]


def get_monero_wallet_settings_service(
    session: DatabaseSession, *, resources: ResourcesDependency
) -> MoneroWalletSettingsService:
    return MoneroWalletSettingsService.on_session(session, wiring=resources.invoice_wiring)


MoneroWalletSettingsServiceDependency = Annotated[
    MoneroWalletSettingsService, Depends(get_monero_wallet_settings_service)
]


def get_invoice_export_service(*, redis: RedisDependency, resources: ResourcesDependency) -> InvoiceExportService:
    """No request session: the request's own commits before the response is sent, and the export streams after."""
    return InvoiceExportService(
        session_factory=resources.database.session_factory, redis=redis, wiring=resources.invoice_wiring
    )


InvoiceExportServiceDependency = Annotated[InvoiceExportService, Depends(get_invoice_export_service)]
