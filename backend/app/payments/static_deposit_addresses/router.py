"""Static deposit address routes, thin: every one by the account's session or its API token, scoped to the account."""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from app.payments.accounts.dependencies import AccountOrApiTokenAccessDependency
from app.payments.accounts.errors import OriginMismatchError
from app.payments.chains.errors import AssetNotSupportedError, ChainKindNotSupportedError, ChainNotEnabledError
from app.payments.invoices.errors import DestinationRefusedError, FactoryNotDeployedError
from app.payments.static_deposit_addresses.dependencies import StaticDepositAddressServiceDependency
from app.payments.static_deposit_addresses.errors import (
    StaticDepositAddressLimitReachedError,
    StaticDepositAddressNotFoundError,
)
from app.payments.static_deposit_addresses.schemas import (
    CUSTOMER_REFERENCE_MAXIMUM_LENGTH,
    CreateStaticDepositAddressRequest,
    StaticDepositAddressListResponse,
    StaticDepositAddressResponse,
)
from app.shared.errors import InvalidInputError, ServiceUnavailableError
from app.shared.openapi_error_responses import describe_error_responses
from app.shared.path_params import API_PREFIX

static_deposit_addresses_router = APIRouter(prefix=API_PREFIX, tags=["static deposit addresses"])

NO_STORE_HEADERS = {"Cache-Control": "no-store"}
# Public ids: ``sda_`` and 22 base58 characters (app/shared/identifiers.py).
StaticDepositAddressIdPath = Annotated[str, Path(pattern=r"^sda_[1-9A-HJ-NP-Za-km-z]{22}$")]


@static_deposit_addresses_router.post(
    "/static-deposit-addresses",
    status_code=201,
    responses=describe_error_responses(
        InvalidInputError,
        DestinationRefusedError,
        ChainNotEnabledError,
        AssetNotSupportedError,
        ChainKindNotSupportedError,
        OriginMismatchError,
        StaticDepositAddressLimitReachedError,
        FactoryNotDeployedError,
        ServiceUnavailableError,
    ),
)
async def create_static_deposit_address(
    creation_request: CreateStaticDepositAddressRequest,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    static_deposit_address_service: StaticDepositAddressServiceDependency,
) -> StaticDepositAddressResponse:
    """The account's permanent address for the customer on the chain; repeating it answers the one already made."""
    return await static_deposit_address_service.create_static_deposit_address(
        creation_request, account_access=account_access
    )


@static_deposit_addresses_router.get(
    "/static-deposit-addresses", responses=describe_error_responses(InvalidInputError, ChainNotEnabledError)
)
async def list_static_deposit_addresses(
    *,
    response: Response,
    account_access: AccountOrApiTokenAccessDependency,
    static_deposit_address_service: StaticDepositAddressServiceDependency,
    chain: str | None = Query(default=None, max_length=32),
    customer_reference: str | None = Query(default=None, min_length=1, max_length=CUSTOMER_REFERENCE_MAXIMUM_LENGTH),
    customer_reference_prefix: str | None = Query(
        default=None, min_length=1, max_length=CUSTOMER_REFERENCE_MAXIMUM_LENGTH
    ),
    cursor: str | None = Query(default=None, max_length=200),
) -> StaticDepositAddressListResponse:
    """Newest first, 50 a page; ``customer_reference`` gives 0 or 1 per chain, ``customer_reference_prefix`` those
    starting with it; not both."""
    response.headers.update(NO_STORE_HEADERS)
    return await static_deposit_address_service.list_static_deposit_addresses(
        account_access.account_id,
        chain_key=chain,
        customer_reference=customer_reference,
        customer_reference_prefix=customer_reference_prefix,
        cursor=cursor,
    )


@static_deposit_addresses_router.get(
    "/static-deposit-addresses/{static_deposit_address_id}",
    responses=describe_error_responses(InvalidInputError, StaticDepositAddressNotFoundError),
)
async def read_static_deposit_address(
    static_deposit_address_id: StaticDepositAddressIdPath,
    *,
    response: Response,
    account_access: AccountOrApiTokenAccessDependency,
    static_deposit_address_service: StaticDepositAddressServiceDependency,
) -> StaticDepositAddressResponse:
    """With its newest 100 payments; another account's is 404."""
    response.headers.update(NO_STORE_HEADERS)
    return await static_deposit_address_service.read_static_deposit_address(
        static_deposit_address_id, account_id=account_access.account_id
    )
