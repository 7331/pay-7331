"""Invoice schedule routes, thin: the account's schedules under the passkey session cookie or its API token, like its
invoices and quantity links."""

from typing import Annotated

from fastapi import APIRouter, Path, Response

from app.payments.accounts.dependencies import AccountOrApiTokenAccessDependency, AccountPreferencesServiceDependency
from app.payments.accounts.errors import OriginMismatchError
from app.payments.accounts.types import AccountAccess
from app.payments.chains.errors import AssetNotSupportedError, ChainKindNotSupportedError, ChainNotEnabledError
from app.payments.invoice_schedules.dependencies import InvoiceScheduleServiceDependency
from app.payments.invoice_schedules.errors import (
    InvoiceScheduleNotFoundError,
    InvoiceScheduleStateError,
    OrderIdPrefixTakenError,
    TooManyInvoiceSchedulesError,
)
from app.payments.invoice_schedules.schemas import (
    CreateInvoiceScheduleRequest,
    InvoiceScheduleInvoiceListResponse,
    InvoiceScheduleListResponse,
    InvoiceScheduleResponse,
)
from app.payments.invoices.errors import DestinationRefusedError, FactoryNotDeployedError
from app.payments.invoices.offered_chains import FIAT_CURRENCIES
from app.shared.errors import InvalidInputError, ServiceUnavailableError
from app.shared.openapi_error_responses import describe_error_responses
from app.shared.path_params import API_PREFIX

invoice_schedules_router = APIRouter(prefix=API_PREFIX, tags=["invoice schedules"])

# `sch_` and 22 base58 characters (app/shared/identifiers.py).
InvoiceScheduleIdPath = Annotated[str, Path(pattern=r"^sch_[1-9A-HJ-NP-Za-km-z]{22}$")]
NO_STORE_HEADERS = {"Cache-Control": "no-store"}
INVOICE_SCHEDULE_READ_ERRORS = (InvalidInputError, InvoiceScheduleNotFoundError)
INVOICE_SCHEDULE_CHANGE_ERROR_RESPONSES = describe_error_responses(
    *INVOICE_SCHEDULE_READ_ERRORS, OriginMismatchError, InvoiceScheduleStateError
)


@invoice_schedules_router.post(
    "/invoice-schedules",
    status_code=201,
    responses=describe_error_responses(
        InvalidInputError,
        DestinationRefusedError,
        ChainNotEnabledError,
        AssetNotSupportedError,
        ChainKindNotSupportedError,
        OriginMismatchError,
        TooManyInvoiceSchedulesError,
        OrderIdPrefixTakenError,
        FactoryNotDeployedError,
        ServiceUnavailableError,
    ),
)
async def create_invoice_schedule(
    schedule_request: CreateInvoiceScheduleRequest,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_schedule_service: InvoiceScheduleServiceDependency,
    account_preferences_service: AccountPreferencesServiceDependency,
) -> InvoiceScheduleResponse:
    """The account's schedule, by session or API token. Made in the browser at a fiat amount, each offered chain's
    coins become the account's starting coins there, as a fiat invoice's do; a server's schedules change no setting."""
    invoice_schedule, offered_asset_symbols_by_chain = await invoice_schedule_service.create_invoice_schedule(
        schedule_request, account_access=account_access
    )
    if isinstance(account_access, AccountAccess) and invoice_schedule.currency in FIAT_CURRENCIES:
        await account_preferences_service.save_accepted_asset_symbols(
            account_access, accepted_asset_symbols_by_chain=offered_asset_symbols_by_chain
        )
    return invoice_schedule


@invoice_schedules_router.get("/invoice-schedules")
async def list_invoice_schedules(
    *,
    response: Response,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_schedule_service: InvoiceScheduleServiceDependency,
) -> InvoiceScheduleListResponse:
    """At most 100: active and paused ones first, then newest first."""
    response.headers.update(NO_STORE_HEADERS)
    return await invoice_schedule_service.list_account_invoice_schedules(account_access.account_id)


@invoice_schedules_router.get(
    "/invoice-schedules/{invoice_schedule_id}", responses=describe_error_responses(*INVOICE_SCHEDULE_READ_ERRORS)
)
async def read_invoice_schedule(
    invoice_schedule_id: InvoiceScheduleIdPath,
    *,
    response: Response,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_schedule_service: InvoiceScheduleServiceDependency,
) -> InvoiceScheduleResponse:
    response.headers.update(NO_STORE_HEADERS)
    return await invoice_schedule_service.read_account_invoice_schedule(
        invoice_schedule_id, account_id=account_access.account_id
    )


@invoice_schedules_router.get(
    "/invoice-schedules/{invoice_schedule_id}/invoices",
    responses=describe_error_responses(*INVOICE_SCHEDULE_READ_ERRORS),
)
async def list_invoice_schedule_invoices(
    invoice_schedule_id: InvoiceScheduleIdPath,
    *,
    response: Response,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_schedule_service: InvoiceScheduleServiceDependency,
) -> InvoiceScheduleInvoiceListResponse:
    """The schedule's invoices, newest period first, at most 200."""
    response.headers.update(NO_STORE_HEADERS)
    return await invoice_schedule_service.list_invoice_schedule_invoices(
        invoice_schedule_id, account_id=account_access.account_id
    )


@invoice_schedules_router.post(
    "/invoice-schedules/{invoice_schedule_id}/pause", responses=INVOICE_SCHEDULE_CHANGE_ERROR_RESPONSES
)
async def pause_invoice_schedule(
    invoice_schedule_id: InvoiceScheduleIdPath,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_schedule_service: InvoiceScheduleServiceDependency,
) -> InvoiceScheduleResponse:
    """Stops making invoices; a period that starts while paused is never billed."""
    return await invoice_schedule_service.pause_invoice_schedule(
        invoice_schedule_id, account_id=account_access.account_id
    )


@invoice_schedules_router.post(
    "/invoice-schedules/{invoice_schedule_id}/resume", responses=INVOICE_SCHEDULE_CHANGE_ERROR_RESPONSES
)
async def resume_invoice_schedule(
    invoice_schedule_id: InvoiceScheduleIdPath,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_schedule_service: InvoiceScheduleServiceDependency,
) -> InvoiceScheduleResponse:
    """Bills again from the next period still to come, not one missed while paused."""
    return await invoice_schedule_service.resume_invoice_schedule(
        invoice_schedule_id, account_id=account_access.account_id
    )


@invoice_schedules_router.post(
    "/invoice-schedules/{invoice_schedule_id}/cancel", responses=INVOICE_SCHEDULE_CHANGE_ERROR_RESPONSES
)
async def cancel_invoice_schedule(
    invoice_schedule_id: InvoiceScheduleIdPath,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_schedule_service: InvoiceScheduleServiceDependency,
) -> InvoiceScheduleResponse:
    """Ends the schedule; invoices already made run their course."""
    return await invoice_schedule_service.cancel_invoice_schedule(
        invoice_schedule_id, account_id=account_access.account_id
    )
