"""Static deposit address request bodies and resources (PLAN.md section 17). Amounts are decimal strings."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.payments.chains.schemas import InvoiceChain
from app.payments.invoices.schemas import MAXIMUM_INVOICE_ASSETS, AssetSymbol
from app.shared.schemas import ApiRequest, ApiResponse

# The merchant's own customer id: letters, digits and ``_ . : @ + -``, as an order id or an email address reads.
CUSTOMER_REFERENCE_PATTERN = r"^[A-Za-z0-9_.:@+\-]{1,128}$"
CUSTOMER_REFERENCE_MAXIMUM_LENGTH = 128


class CreateStaticDepositAddressRequest(ApiRequest):
    customer_reference: str = Field(pattern=CUSTOMER_REFERENCE_PATTERN, max_length=CUSTOMER_REFERENCE_MAXIMUM_LENGTH)
    # The chain's key; left out, the first chain ``GET /chains`` lists.
    chain: str | None = Field(default=None, max_length=32)
    # The coins it credits; left out, the account's ticked coins on that chain.
    assets: list[AssetSymbol] | None = Field(default=None, min_length=1, max_length=MAXIMUM_INVOICE_ASSETS)
    # The payout address may be a contract (a Safe) only when this is set, as for an invoice (PLAN.md section 3).
    allow_contract_destination: bool = False


class StaticDepositAddressPayment(ApiResponse):
    id: int
    asset_symbol: str
    amount: str
    # Null when uncredited money found no price; a credited payment always has one.
    amount_usd: Decimal | None
    asset_price_usd: Decimal | None
    credited: bool
    uncredited_reason: Literal["wrong_coin"] | None
    tx_hash: str | None
    sender: str | None
    block_number: int
    created_at: datetime


class StaticDepositAddressResponse(ApiResponse):
    id: str
    customer_reference: str
    chain: InvoiceChain
    assets: list[str]
    deposit_address: str
    destination_address: str
    sweep_blocked: bool
    # The credited payments' USD sum.
    received_usd: Decimal
    payment_count: int
    last_payment_at: datetime | None
    created_at: datetime
    # Only on ``GET /static-deposit-addresses/{id}``, newest first; ``[]`` elsewhere.
    payments: list[StaticDepositAddressPayment]


class StaticDepositAddressListResponse(ApiResponse):
    static_deposit_addresses: list[StaticDepositAddressResponse]
    next_cursor: str | None


class StaticDepositAddressSweep(ApiResponse):
    tx_hash: str | None
    status: str
