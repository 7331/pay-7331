"""Invoice schedule request bodies and resources. Amounts are decimal strings."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, JsonValue

from app.payments.invoice_schedules.enums import InvoiceScheduleInterval, InvoiceScheduleStatus
from app.payments.invoices.schemas import (
    ADDRESS_PATTERN,
    DECIMAL_TEXT_PATTERN,
    MAXIMUM_INVOICE_ASSETS,
    MAXIMUM_LIFETIME_SECONDS,
    MAXIMUM_OFFERED_CHAINS,
    MINIMUM_LIFETIME_SECONDS,
    AssetsByChain,
    AssetSymbol,
    ChainKey,
    InvoiceAccountResponse,
    InvoiceMetadata,
    RequestTime,
)
from app.shared.schemas import ApiRequest, ApiResponse, IdentifierText, MultilineText, WebPageUrl

# The payer gets the link from the account's server, not at checkout, so a scheduled invoice waits the longest an
# invoice may (12 hours); `refresh_expired` on `POST /invoices` makes a new one after.
DEFAULT_SCHEDULED_INVOICE_LIFETIME_SECONDS = MAXIMUM_LIFETIME_SECONDS
# An order id is at most 128 characters: the prefix, a dash and eight digits of the day fit.
MAXIMUM_ORDER_ID_PREFIX_LENGTH = 100
MAXIMUM_ENDS_AFTER_INVOICE_COUNT = 10_000


class CreateInvoiceScheduleRequest(ApiRequest):
    """``POST /invoices``' fields for every invoice (no ``order_id``, ``refresh_expired`` or
    ``underpaid_open_until``: each invoice gets its own order id, and an absolute time would not repeat), plus when."""

    amount: str = Field(pattern=DECIMAL_TEXT_PATTERN, max_length=40)
    currency: str = Field(min_length=3, max_length=16)
    chain: str | None = Field(default=None, max_length=32)
    chains: list[ChainKey] | None = Field(default=None, min_length=1, max_length=MAXIMUM_OFFERED_CHAINS)
    assets: list[AssetSymbol] | None = Field(default=None, min_length=1, max_length=MAXIMUM_INVOICE_ASSETS)
    assets_by_chain: AssetsByChain | None = None
    destination_address: str | None = Field(default=None, pattern=ADDRESS_PATTERN)
    allow_contract_destination: bool = False
    note: MultilineText = Field(default="", max_length=500)
    metadata: InvoiceMetadata | None = None
    # Each invoice's ``order_id`` is ``<order_id_prefix>-<YYYYMMDD>`` of its period's start (UTC); the schedule's id
    # when left out.
    order_id_prefix: IdentifierText | None = Field(
        default=None, min_length=1, max_length=MAXIMUM_ORDER_ID_PREFIX_LENGTH
    )
    lifetime_seconds: int = Field(
        default=DEFAULT_SCHEDULED_INVOICE_LIFETIME_SECONDS, ge=MINIMUM_LIFETIME_SECONDS, le=MAXIMUM_LIFETIME_SECONDS
    )
    accuracy_percent: Decimal = Field(default=Decimal("1.0"), ge=0, le=5, max_digits=4, decimal_places=2)
    return_url: WebPageUrl | None = None
    success_url: WebPageUrl | None = None
    callback_url: str | None = Field(default=None, min_length=1, max_length=2048)
    interval: InvoiceScheduleInterval
    # The first period's start (a time without a zone is UTC); now when left out, so the first invoice comes at once.
    starts_at: RequestTime | None = None
    # At most one end: after this many invoices, or no period starting at or after this time. Neither: until cancelled.
    ends_after_invoice_count: int | None = Field(default=None, ge=1, le=MAXIMUM_ENDS_AFTER_INVOICE_COUNT)
    ends_at: RequestTime | None = None


class InvoiceScheduleResponse(ApiResponse):
    id: str
    status: InvoiceScheduleStatus
    interval: InvoiceScheduleInterval
    starts_at: datetime
    ends_after_invoice_count: int | None
    ends_at: datetime | None
    # The start of the next period to bill; null once ended (and while paused, until it is resumed).
    next_period_start: datetime | None
    created_invoice_count: int
    last_invoice_created_at: datetime | None
    # Why the last attempt made no invoice (an API error code, e.g. `price_unavailable`); null after a success.
    last_failure_code: str | None
    last_failure_at: datetime | None
    amount: str
    currency: str
    # The chains and coins every invoice offers, as resolved when the schedule was made.
    chains: list[str]
    assets: list[str]
    assets_by_chain: dict[str, list[str]] | None
    destination_address: str
    note: str
    metadata: dict[str, JsonValue] | None
    order_id_prefix: str
    lifetime_seconds: int
    accuracy_percent: str
    return_url: str | None
    success_url: str | None
    callback_url: str | None
    paused_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime


class InvoiceScheduleListResponse(ApiResponse):
    invoice_schedules: list[InvoiceScheduleResponse]


class InvoiceScheduleInvoiceListResponse(ApiResponse):
    # Newest period first, at most 200; `GET /invoices?order_id_prefix=` pages through all of them.
    invoices: list[InvoiceAccountResponse]
