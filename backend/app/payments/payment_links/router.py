"""Quantity link routes, thin: the account's links under the passkey session cookie or its API token, and the payer's
public, rate-limited routes."""

from typing import Annotated

from fastapi import APIRouter, Path, Response

from app.lifespan.dependencies import ClientIpAddressDependency
from app.payments.accounts.dependencies import (
    AccountOrApiTokenAccessDependency,
    AccountPreferencesServiceDependency,
)
from app.payments.accounts.errors import OriginMismatchError
from app.payments.accounts.types import AccountAccess
from app.payments.chains.errors import AssetNotSupportedError, ChainKindNotSupportedError, ChainNotEnabledError
from app.payments.invoices.errors import (
    DestinationRefusedError,
    FactoryNotDeployedError,
    InvoiceStateError,
    TooManyOpenInvoicesError,
)
from app.payments.invoices.schemas import InvoicePublicResponse
from app.payments.monero_wallets.errors import (
    MoneroLookaheadExhaustedError,
    MoneroSidecarUnavailableError,
    MoneroWalletMissingError,
)
from app.payments.payment_links.dependencies import PaymentLinkServiceDependency
from app.payments.payment_links.errors import PaymentLinkClosedError, PaymentLinkNotFoundError, PaymentLinkSoldOutError
from app.payments.payment_links.schemas import (
    CreatePaymentLinkInvoiceRequest,
    CreatePaymentLinkRequest,
    PaymentLinkAccountResponse,
    PaymentLinkDetailResponse,
    PaymentLinkListResponse,
    PaymentLinkPublicResponse,
)
from app.payments.price_quotes.errors import PriceUnavailableError
from app.shared.errors import InvalidInputError, ServiceUnavailableError
from app.shared.openapi_error_responses import describe_error_responses
from app.shared.path_params import API_PREFIX

payment_links_router = APIRouter(prefix=API_PREFIX, tags=["payment links"])

# `lnk_` and 22 base58 characters (app/shared/identifiers.py).
PaymentLinkIdPath = Annotated[str, Path(pattern=r"^lnk_[1-9A-HJ-NP-Za-km-z]{22}$")]
NO_STORE_HEADERS = {"Cache-Control": "no-store"}
PAYMENT_LINK_READ_ERRORS = (InvalidInputError, PaymentLinkNotFoundError)


@payment_links_router.post(
    "/payment-links",
    status_code=201,
    responses=describe_error_responses(
        InvalidInputError,
        DestinationRefusedError,
        ChainNotEnabledError,
        AssetNotSupportedError,
        ChainKindNotSupportedError,
        OriginMismatchError,
        MoneroWalletMissingError,
        FactoryNotDeployedError,
        MoneroSidecarUnavailableError,
        ServiceUnavailableError,
    ),
)
async def create_payment_link(
    link_request: CreatePaymentLinkRequest,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    payment_link_service: PaymentLinkServiceDependency,
    account_preferences_service: AccountPreferencesServiceDependency,
) -> PaymentLinkAccountResponse:
    """The account's quantity link, by session or API token. Made in the browser, each offered chain's coins become
    the account's starting coins there, as a fiat invoice's do; a server's links change no setting."""
    payment_link, offered_assets_by_chain = await payment_link_service.create_payment_link(
        link_request, account_id=account_access.account_id, payout_address=account_access.payout_address
    )
    if isinstance(account_access, AccountAccess):
        await account_preferences_service.save_accepted_asset_symbols(
            account_access, accepted_asset_symbols_by_chain=offered_assets_by_chain
        )
    return payment_link


@payment_links_router.get("/payment-links")
async def list_payment_links(
    *, account_access: AccountOrApiTokenAccessDependency, payment_link_service: PaymentLinkServiceDependency
) -> PaymentLinkListResponse:
    return await payment_link_service.list_account_payment_links(account_access.account_id)


@payment_links_router.get(
    "/payment-links/{payment_link_id}", responses=describe_error_responses(*PAYMENT_LINK_READ_ERRORS)
)
async def read_payment_link(
    payment_link_id: PaymentLinkIdPath,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    payment_link_service: PaymentLinkServiceDependency,
) -> PaymentLinkDetailResponse:
    return await payment_link_service.read_account_payment_link(payment_link_id, account_id=account_access.account_id)


@payment_links_router.post(
    "/payment-links/{payment_link_id}/close",
    responses=describe_error_responses(*PAYMENT_LINK_READ_ERRORS, OriginMismatchError),
)
async def close_payment_link(
    payment_link_id: PaymentLinkIdPath,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    payment_link_service: PaymentLinkServiceDependency,
) -> PaymentLinkAccountResponse:
    """No new payer after this; invoices already made run their course."""
    return await payment_link_service.close_payment_link(payment_link_id, account_id=account_access.account_id)


@payment_links_router.get(
    "/public/payment-links/{payment_link_id}", responses=describe_error_responses(*PAYMENT_LINK_READ_ERRORS)
)
async def read_public_payment_link(
    payment_link_id: PaymentLinkIdPath, *, response: Response, payment_link_service: PaymentLinkServiceDependency
) -> PaymentLinkPublicResponse:
    response.headers.update(NO_STORE_HEADERS)
    return await payment_link_service.read_public_payment_link(payment_link_id)


@payment_links_router.post(
    "/public/payment-links/{payment_link_id}/invoices",
    status_code=201,
    responses=describe_error_responses(
        *PAYMENT_LINK_READ_ERRORS,
        TooManyOpenInvoicesError,
        PaymentLinkSoldOutError,
        PaymentLinkClosedError,
        InvoiceStateError,
        MoneroWalletMissingError,
        MoneroLookaheadExhaustedError,
        FactoryNotDeployedError,
        PriceUnavailableError,
        MoneroSidecarUnavailableError,
        ServiceUnavailableError,
    ),
)
async def create_payment_link_invoice(
    payment_link_id: PaymentLinkIdPath,
    *,
    invoice_request: CreatePaymentLinkInvoiceRequest,
    client_ip_address: ClientIpAddressDependency,
    payment_link_service: PaymentLinkServiceDependency,
) -> InvoicePublicResponse:
    """The payer's own invoice in the picked coin on the picked chain, its quote live at once: takes a slot."""
    return await payment_link_service.create_payment_link_invoice(
        payment_link_id,
        chain_key=invoice_request.chain,
        asset_symbol=invoice_request.asset,
        client_ip_address=client_ip_address,
    )
