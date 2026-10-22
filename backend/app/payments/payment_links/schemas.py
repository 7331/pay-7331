"""Quantity link request bodies and resources. Amounts are decimal strings."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator, Field

from app.payments.chains.schemas import InvoiceChain
from app.payments.invoices.enums import FiatCurrency
from app.payments.invoices.schemas import (
    ADDRESS_PATTERN,
    DECIMAL_TEXT_PATTERN,
    DEFAULT_LIFETIME_SECONDS,
    MAXIMUM_INVOICE_ASSETS,
    MAXIMUM_LIFETIME_SECONDS,
    MAXIMUM_OFFERED_CHAINS,
    MINIMUM_LIFETIME_SECONDS,
    AssetsByChain,
    AssetSymbol,
    ChainKey,
    InvoiceAccountResponse,
    InvoiceFiat,
    PaymentOption,
)
from app.payments.payment_links.enums import PaymentLinkStatus
from app.shared.schemas import ApiRequest, ApiResponse, MultilineText, WebPageUrl

MAXIMUM_PAYMENT_LINK_QUANTITY = 1000


def uppercase_text(value: object) -> object:
    """``usd`` reads as ``USD``, as it always has; anything not text is left for the enum to refuse."""
    match value:
        case str():
            return value.upper()
        case _:
            return value


# USD or EUR, in any case: a coin-priced link is not offered.
CaseInsensitiveFiatCurrency = Annotated[FiatCurrency, BeforeValidator(uppercase_text)]


class CreatePaymentLinkRequest(ApiRequest):
    amount: str = Field(pattern=DECIMAL_TEXT_PATTERN, max_length=40)
    currency: CaseInsensitiveFiatCurrency
    # One of ``chain`` (one chain, as ever), ``chains`` (several EVM chains the payer picks from) or the chains
    # ``assets_by_chain`` names.
    chain: str | None = Field(default=None, max_length=32)
    chains: list[ChainKey] | None = Field(default=None, min_length=1, max_length=MAXIMUM_OFFERED_CHAINS)
    # Every chain takes the coins of ``assets`` it lists; a chain in ``assets_by_chain`` takes exactly its own list.
    assets: list[AssetSymbol] | None = Field(default=None, min_length=1, max_length=MAXIMUM_INVOICE_ASSETS)
    assets_by_chain: AssetsByChain | None = None
    destination_address: str | None = Field(default=None, pattern=ADDRESS_PATTERN)
    allow_contract_destination: bool = False
    note: MultilineText = Field(default="", max_length=500)
    quantity: int = Field(ge=1, le=MAXIMUM_PAYMENT_LINK_QUANTITY)
    lifetime_seconds: int = Field(
        default=DEFAULT_LIFETIME_SECONDS, ge=MINIMUM_LIFETIME_SECONDS, le=MAXIMUM_LIFETIME_SECONDS
    )
    accuracy_percent: Decimal = Field(default=Decimal("1.0"), ge=0, le=5, max_digits=4, decimal_places=2)
    return_url: WebPageUrl | None = None


class CreatePaymentLinkInvoiceRequest(ApiRequest):
    """The payer's pick: ``chain`` may be left out only when the coin is offered on one chain."""

    asset: AssetSymbol
    chain: str | None = Field(default=None, max_length=32)


class PaymentLinkPublicResponse(ApiResponse):
    id: str
    url: str
    status: PaymentLinkStatus
    quantity: int
    slots_left: int
    # The first offered chain and its coins: for clients that know one chain; `payment_options` has every chain.
    chain: InvoiceChain
    assets: list[str]
    # Every coin on every offered chain with its estimate; a chain whose feeds do not answer has no amounts, the rest do.
    payment_options: list[PaymentOption]
    fiat: InvoiceFiat
    note: str
    created_at: datetime


class PaymentLinkAccountResponse(PaymentLinkPublicResponse):
    destination_address: str
    lifetime_seconds: int
    accuracy_percent: str
    return_url: str | None
    # Invoices paid, underpaid, confirming or still open; paid ones are `paid_count`.
    slots_held: int
    paid_count: int
    closed_at: datetime | None


class PaymentLinkDetailResponse(PaymentLinkAccountResponse):
    # Newest first, at most 200.
    invoices: list[InvoiceAccountResponse]


class PaymentLinkListResponse(ApiResponse):
    payment_links: list[PaymentLinkAccountResponse]
