"""Invoice routes, thin: anyone's one-tap invoice, the signed-in account's own invoices under the passkey session
cookie or its API token, and the payer's public, rate-limited routes. Refunds stay session only: they pay from the
operator float."""

from datetime import UTC, datetime

from fastapi import APIRouter, Query, Response
from fastapi.responses import StreamingResponse

from app.lifespan.dependencies import ClientIpAddressDependency
from app.payments.accounts.dependencies import (
    AccountAccessDependency,
    AccountOrApiTokenAccessDependency,
    AccountPreferencesServiceDependency,
    OptionalAccountAccessDependency,
)
from app.payments.accounts.errors import OriginMismatchError
from app.payments.accounts.types import AccountAccess, AccountApiTokenAccess
from app.payments.chains.errors import AssetNotSupportedError, ChainKindNotSupportedError, ChainNotEnabledError
from app.payments.invoices.dependencies import (
    InvoiceExportServiceDependency,
    InvoiceServiceDependency,
    PaymentChecksServiceDependency,
)
from app.payments.invoices.enums import InvoiceCreationChannel, InvoiceStatus, QrKind
from app.payments.invoices.errors import (
    DestinationRefusedError,
    DestinationRequiredError,
    FactoryNotDeployedError,
    InvoiceNotFoundError,
    InvoiceNotRefreshableError,
    InvoiceStateError,
    PublicInvoiceCapacityReachedError,
    RefundNotAllowedError,
    TooManyOpenInvoicesError,
)
from app.payments.invoices.offered_chains import (
    list_every_asset_symbol,
    read_requested_chain_keys,
    resolve_account_invoice_offer,
)
from app.payments.invoices.schemas import (
    AccountRefundRequest,
    ChooseAssetRequest,
    CreateInvoiceRequest,
    CreatePublicInvoiceRequest,
    InvoiceAccountResponse,
    InvoicePageResponse,
    InvoicePublicResponse,
    PaymentCheckRequest,
    PaymentCheckResponse,
    RefundOfferListResponse,
    RefundRequestRequest,
)
from app.payments.invoices.types import NewInvoice
from app.payments.monero_wallets.errors import (
    MoneroLookaheadExhaustedError,
    MoneroSidecarUnavailableError,
    MoneroWalletChangeConflictError,
    MoneroWalletMissingError,
)
from app.payments.price_quotes.errors import PriceUnavailableError
from app.payments.settlements.errors import RefundOfferNotFoundError, RefundOfferUsedError, RefundSignatureInvalidError
from app.shared.amounts import parse_decimal_amount
from app.shared.errors import InvalidInputError, ServiceUnavailableError
from app.shared.openapi_error_responses import describe_error_responses
from app.shared.path_params import API_PREFIX, InvoiceIdPath

invoices_router = APIRouter(prefix=API_PREFIX, tags=["invoices"])

NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class SvgImageResponse(Response):
    """An SVG image: its media type set on the class, so the OpenAPI document names it
    (https://fastapi.tiangolo.com/advanced/custom-response/)."""

    media_type = "image/svg+xml"


class CsvStreamingResponse(StreamingResponse):
    """CSV text, streamed; Starlette adds ``charset=utf-8`` to the header of a ``text/`` type."""

    media_type = "text/csv"


# Every refusal of making an invoice that the request alone can cause, with or without an account.
INVOICE_CREATION_ERRORS = (
    InvalidInputError,
    DestinationRefusedError,
    ChainNotEnabledError,
    AssetNotSupportedError,
    ChainKindNotSupportedError,
    TooManyOpenInvoicesError,
    FactoryNotDeployedError,
    PriceUnavailableError,
    ServiceUnavailableError,
)
# The payer's routes on an invoice another party made.
PUBLIC_INVOICE_ERRORS = (InvalidInputError, InvoiceNotFoundError)


@invoices_router.post(
    "/public/invoices",
    status_code=201,
    responses=describe_error_responses(
        DestinationRequiredError, *INVOICE_CREATION_ERRORS, PublicInvoiceCapacityReachedError
    ),
)
async def create_public_invoice(
    invoice_request: CreatePublicInvoiceRequest,
    *,
    account_access: OptionalAccountAccessDependency,
    client_ip_address: ClientIpAddressDependency,
    invoice_service: InvoiceServiceDependency,
    account_preferences_service: AccountPreferencesServiceDependency,
) -> InvoicePublicResponse:
    """Anyone's USD invoice on every enabled EVM chain with a factory (or the ``chains`` or ``assets_by_chain`` named),
    in the ticked assets or else every asset of each; the payer picks the chain and coin. Signed out the receiver
    address is required; signed in it defaults to the account's payout address, the invoice is the
    account's and each offered chain's coins become the account's starting coins there, and each offered chain takes the
    account's saved payment window and shortfall for it (else the server's: 30 minutes, 1%)."""
    destination_address = invoice_request.destination_address or (
        account_access.payout_address if account_access is not None else None
    )
    if destination_address is None:
        raise DestinationRequiredError("destination_address: paste the receiver's 0x address")
    defaulted_chains = invoice_request.chains is None and invoice_request.assets_by_chain is None
    chain_keys = (
        invoice_service.list_invoiceable_evm_chain_keys()
        if defaulted_chains
        else read_requested_chain_keys(
            chain=None, chains=invoice_request.chains, assets_by_chain=invoice_request.assets_by_chain
        )
    )
    invoice = await invoice_service.create_invoice(
        NewInvoice(
            chain_keys=chain_keys,
            defaulted_chains=defaulted_chains,
            amount=parse_decimal_amount(invoice_request.amount_usd, field_name="amount_usd"),
            currency="USD",
            assets=invoice_request.assets
            or (
                []
                if invoice_request.assets_by_chain
                else list_every_asset_symbol(invoice_service.chains_service, chain_keys=chain_keys)
            ),
            assets_by_chain=invoice_request.assets_by_chain,
            destination_address=destination_address,
            account_id=account_access.account_id if account_access is not None else None,
            note=invoice_request.note,
            # Signed in, each chain's payment window and shortfall are the account's saved ones for it, else the
            # server's; signed out, the server's.
        ),
        client_ip_address=client_ip_address,
    )
    if account_access is not None:
        await account_preferences_service.save_accepted_asset_symbols(
            account_access, accepted_asset_symbols_by_chain=invoice_service.offered_assets_of_invoice(invoice)
        )
    return await invoice_service.read_public_invoice(invoice.id)


@invoices_router.post(
    "/invoices",
    status_code=201,
    responses=describe_error_responses(
        *INVOICE_CREATION_ERRORS,
        OriginMismatchError,
        InvoiceNotRefreshableError,
        MoneroWalletMissingError,
        MoneroWalletChangeConflictError,
        MoneroLookaheadExhaustedError,
        MoneroSidecarUnavailableError,
    ),
)
async def create_account_invoice(
    invoice_request: CreateInvoiceRequest,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    client_ip_address: ClientIpAddressDependency,
    invoice_service: InvoiceServiceDependency,
    account_preferences_service: AccountPreferencesServiceDependency,
) -> InvoiceAccountResponse:
    """The account's invoice, by its session or its API token. A repeated ``order_id`` answers the
    invoice made with it. Made in the browser, a fiat one's coins become the account's starting coins (a
    coin amount's single coin was no pick, so it saves nothing); a server's invoices change no
    setting. With no network field it offers every enabled EVM chain; with no coin field a fiat invoice takes the
    account's saved coins on each chain and a coin amount its own coin, so ``amount`` and ``currency`` alone make an
    invoice. ``lifetime_seconds`` and ``accuracy_percent`` apply to every offered chain; left out, each chain takes the
    account's saved default for it, else the server's (30 minutes, 1%)."""
    is_api_token_request = isinstance(account_access, AccountApiTokenAccess)
    currency = invoice_request.currency.upper()
    offer = resolve_account_invoice_offer(
        invoiceable_evm_chain_keys=invoice_service.list_invoiceable_evm_chain_keys(),
        chain=invoice_request.chain,
        chains=invoice_request.chains,
        assets=invoice_request.assets,
        assets_by_chain=invoice_request.assets_by_chain,
        currency=currency,
        accepted_asset_symbols_by_chain=account_access.accepted_asset_symbols_by_chain,
    )
    invoice = await invoice_service.create_invoice(
        NewInvoice(
            chain_keys=offer.chain_keys,
            defaulted_chains=offer.defaulted_chains,
            amount=parse_decimal_amount(invoice_request.amount, field_name="amount"),
            currency=currency,
            assets=offer.assets,
            assets_by_chain=offer.assets_by_chain,
            destination_address=invoice_request.destination_address or account_access.payout_address,
            account_id=account_access.account_id,
            allow_contract_destination=invoice_request.allow_contract_destination,
            note=invoice_request.note,
            order_id=invoice_request.order_id,
            lifetime_seconds=invoice_request.lifetime_seconds,
            accuracy_percent=invoice_request.accuracy_percent,
            underpaid_open_until=invoice_request.underpaid_open_until,
            return_url=invoice_request.return_url,
            success_url=invoice_request.success_url,
            callback_url=invoice_request.callback_url,
            metadata=invoice_request.metadata,
            refresh_expired=invoice_request.refresh_expired,
        ),
        client_ip_address=client_ip_address,
        creation_channel=InvoiceCreationChannel.API_TOKEN if is_api_token_request else InvoiceCreationChannel.WEB,
    )
    if isinstance(account_access, AccountAccess) and invoice.fiat_currency is not None:
        await account_preferences_service.save_accepted_asset_symbols(
            account_access, accepted_asset_symbols_by_chain=invoice_service.offered_assets_of_invoice(invoice)
        )
    return await invoice_service.read_account_invoice(invoice.id, account_id=account_access.account_id)


@invoices_router.get("/invoices", responses=describe_error_responses(InvalidInputError))
async def list_account_invoices(
    *,
    response: Response,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_service: InvoiceServiceDependency,
    status: InvoiceStatus | None = None,
    cursor: str | None = Query(default=None, max_length=200),
    order_id: str | None = Query(default=None, min_length=1, max_length=128),
    order_id_prefix: str | None = Query(default=None, min_length=1, max_length=128),
) -> InvoicePageResponse:
    """By session or API token. ``order_id`` narrows it to the one invoice made with it (0 or 1 items),
    ``order_id_prefix`` to those whose ``order_id`` starts with it (paged); not both."""
    response.headers.update(NO_STORE_HEADERS)
    return await invoice_service.list_account_invoices(
        account_access.account_id, status=status, cursor=cursor, order_id=order_id, order_id_prefix=order_id_prefix
    )


@invoices_router.get(
    "/invoices/export.csv", response_class=CsvStreamingResponse, responses=describe_error_responses(InvalidInputError)
)
async def export_account_invoices_csv(
    *,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_export_service: InvoiceExportServiceDependency,
    status: InvoiceStatus | None = None,
    created_from: datetime | None = None,
    created_before: datetime | None = None,
) -> CsvStreamingResponse:
    """The account's invoices and their payments, wrong-coin payments, sweeps and refunds as CSV, by session or API
    token, oldest first, streamed. ``created_from`` (inclusive) and ``created_before`` (exclusive) bound the invoices'
    creation time; a time without a zone is UTC."""
    # Declared before ``/invoices/{invoice_id}``, which would otherwise match the path.
    if created_from is not None and created_from.tzinfo is None:
        created_from = created_from.replace(tzinfo=UTC)
    if created_before is not None and created_before.tzinfo is None:
        created_before = created_before.replace(tzinfo=UTC)
    if created_from is not None and created_before is not None and created_from >= created_before:
        raise InvalidInputError("created_before: must be after created_from")
    file_name = f"pay-7331-invoices-{datetime.now(UTC):%Y%m%d}.csv"
    return CsvStreamingResponse(
        invoice_export_service.stream_account_invoices_csv(
            account_access.account_id, status=status, created_from=created_from, created_before=created_before
        ),
        headers={**NO_STORE_HEADERS, "Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@invoices_router.get("/invoices/{invoice_id}", responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS))
async def read_account_invoice(
    invoice_id: InvoiceIdPath,
    *,
    response: Response,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_service: InvoiceServiceDependency,
) -> InvoiceAccountResponse:
    """By session or API token; another account's invoice is 404."""
    response.headers.update(NO_STORE_HEADERS)
    return await invoice_service.read_account_invoice(invoice_id, account_id=account_access.account_id)


@invoices_router.post(
    "/invoices/{invoice_id}/cancel",
    responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS, OriginMismatchError, InvoiceStateError),
)
async def cancel_invoice(
    invoice_id: InvoiceIdPath,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_service: InvoiceServiceDependency,
) -> InvoiceAccountResponse:
    """By session or API token: only an invoice with nothing received, so no money moves."""
    return await invoice_service.cancel_invoice(invoice_id, account_id=account_access.account_id)


@invoices_router.post(
    "/invoices/{invoice_id}/accept-partial",
    responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS, OriginMismatchError, InvoiceStateError),
)
async def accept_partial_payment(
    invoice_id: InvoiceIdPath,
    *,
    account_access: AccountOrApiTokenAccessDependency,
    invoice_service: InvoiceServiceDependency,
) -> InvoiceAccountResponse:
    """By session or API token: the money goes only to the destination the invoice was made with."""
    return await invoice_service.accept_partial(invoice_id, account_id=account_access.account_id)


@invoices_router.post(
    "/invoices/{invoice_id}/refunds",
    responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS, OriginMismatchError, RefundNotAllowedError),
)
async def create_account_refund(
    invoice_id: InvoiceIdPath,
    *,
    refund_request: AccountRefundRequest,
    account_access: AccountAccessDependency,
    invoice_service: InvoiceServiceDependency,
) -> InvoiceAccountResponse:
    return await invoice_service.create_account_refund(
        invoice_id,
        account_id=account_access.account_id,
        sender=refund_request.sender,
        amount_text=refund_request.amount,
    )


@invoices_router.get("/public/invoices/{invoice_id}", responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS))
async def read_public_invoice(
    invoice_id: InvoiceIdPath, *, response: Response, invoice_service: InvoiceServiceDependency
) -> InvoicePublicResponse:
    response.headers.update(NO_STORE_HEADERS)
    return await invoice_service.read_public_invoice(invoice_id)


@invoices_router.get(
    "/public/invoices/{invoice_id}/qr.svg",
    response_class=SvgImageResponse,
    responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS, InvoiceStateError),
)
async def read_invoice_qr_code(
    invoice_id: InvoiceIdPath, *, invoice_service: InvoiceServiceDependency, kind: QrKind = QrKind.PAYMENT
) -> SvgImageResponse:
    """The payment URI (or only the deposit address) as a QR code, once a coin is picked."""
    svg = await invoice_service.render_invoice_qr_svg(invoice_id, address_only=kind == QrKind.ADDRESS)
    return SvgImageResponse(content=svg, headers=NO_STORE_HEADERS)


@invoices_router.post(
    "/public/invoices/{invoice_id}/asset",
    responses=describe_error_responses(
        *PUBLIC_INVOICE_ERRORS,
        ChainKindNotSupportedError,
        InvoiceStateError,
        FactoryNotDeployedError,
        PriceUnavailableError,
        ServiceUnavailableError,
    ),
)
async def choose_invoice_asset(
    invoice_id: InvoiceIdPath,
    *,
    asset_request: ChooseAssetRequest,
    invoice_service: InvoiceServiceDependency,
) -> InvoicePublicResponse:
    """The payer's pick of a coin on a chain: starts the quote and the payment window."""
    return await invoice_service.choose_asset(
        invoice_id, chain_key=asset_request.chain, asset_symbol=asset_request.asset
    )


@invoices_router.post(
    "/public/invoices/{invoice_id}/quote",
    responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS, InvoiceStateError, PriceUnavailableError),
)
async def refresh_invoice_quote(
    invoice_id: InvoiceIdPath, *, invoice_service: InvoiceServiceDependency
) -> InvoicePublicResponse:
    """A fresh quote for the rest of an open underpaid fiat invoice."""
    return await invoice_service.refresh_quote(invoice_id)


@invoices_router.post(
    "/public/invoices/{invoice_id}/payment-checks",
    responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS, InvoiceStateError, ServiceUnavailableError),
)
async def check_invoice_payment(
    invoice_id: InvoiceIdPath,
    *,
    check_request: PaymentCheckRequest,
    payment_checks_service: PaymentChecksServiceDependency,
) -> PaymentCheckResponse:
    """The payer's "I paid": rereads the deposit, or looks up a pasted transaction hash. Rate limited as a public
    write; records only what the chain proves and never credits the invoice."""
    return await payment_checks_service.check_invoice_payment(
        invoice_id, transaction_hash_text=check_request.transaction_hash
    )


@invoices_router.post(
    "/public/invoices/{invoice_id}/refund-offer",
    responses=describe_error_responses(*PUBLIC_INVOICE_ERRORS, RefundNotAllowedError, PriceUnavailableError),
)
async def create_refund_offer(
    invoice_id: InvoiceIdPath, *, invoice_service: InvoiceServiceDependency
) -> RefundOfferListResponse:
    """A message per refundable sender, to sign with the wallet that paid; each offer is short-lived."""
    return await invoice_service.create_refund_offers(invoice_id)


@invoices_router.post(
    "/public/invoices/{invoice_id}/refund-requests",
    responses=describe_error_responses(
        *PUBLIC_INVOICE_ERRORS,
        RefundSignatureInvalidError,
        RefundOfferNotFoundError,
        RefundOfferUsedError,
        RefundNotAllowedError,
    ),
)
async def request_refund(
    invoice_id: InvoiceIdPath,
    *,
    refund_request: RefundRequestRequest,
    invoice_service: InvoiceServiceDependency,
) -> InvoicePublicResponse:
    """A refund of an offer, signed by the sender it names."""
    return await invoice_service.request_signed_refund(
        invoice_id, nonce=refund_request.nonce, signature=refund_request.signature
    )
