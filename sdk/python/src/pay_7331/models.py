"""Typed responses of the pay-7331 API, field for field as the backend's schemas send them
(backend/app/payments/*/schemas.py; PLAN.md sections 7.1, 8.1 and 17).

pydantic v2 models (https://docs.pydantic.dev/latest/concepts/models/). Amounts cross the wire as decimal strings and
become ``decimal.Decimal`` here, which pydantic builds from the string exactly
(https://docs.pydantic.dev/latest/api/standard_library_types/#decimaldecimal), so ``str(invoice.amount_due)`` is the
API's text and no amount is ever a float. Times are timezone-aware ``datetime``. Statuses stay ``str``, not enums: a
status the API adds later must not break an older SDK; so do the other kinds and reasons. Unknown fields are ignored for the same reason. Every model is
frozen.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from pay_7331.errors import PayEventDataError

INVOICE_STATUSES_PAID = frozenset({"paid", "overpaid"})


class PayModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Asset(PayModel):
    symbol: str
    name: str
    decimals: int
    # None for the chain's native coin.
    token_address: str | None


class Chain(PayModel):
    """A network. In ``list_chains()`` it carries ``assets``, ``default_accepted_assets`` and the payment window and
    shortfall an invoice on it takes when neither its maker nor its account set one; inside an invoice, link or
    customer address the API sends none of them and they are None."""

    key: str
    name: str
    # evm or monero.
    kind: str
    # None on a Monero chain.
    chain_id: int | None
    explorer_url: str
    expected_finality_seconds: int
    confirmations_final: int | None = None
    assets: list[Asset] | None = None
    default_accepted_assets: list[str] | None = None
    default_invoice_lifetime_seconds: int | None = None
    default_accuracy_percent: Decimal | None = None


class ExchangeRate(PayModel):
    """USD and EUR per whole coin, as a new quote would price it now; both None while the chain's feeds are stale."""

    chain: str
    asset: str
    usd: Decimal | None
    eur: Decimal | None


class Fiat(PayModel):
    """``currency`` and ``amount`` are None on a coin-priced invoice; ``usd`` and ``eur`` None only when no feed
    answers."""

    currency: str | None
    amount: Decimal | None
    usd: Decimal | None
    eur: Decimal | None


class PaymentOption(PayModel):
    """One coin on one network an ``awaiting_asset`` invoice or a link offers; ``amount`` None while that chain's
    feeds do not answer. Not a quote."""

    chain: Chain
    asset: Asset
    amount: Decimal | None


class Quote(PayModel):
    asset_price_usd: Decimal
    expires_at: datetime


class Payment(PayModel):
    tx_hash: str | None
    sender: str | None
    amount: Decimal
    block_number: int
    final: bool


class Sweep(PayModel):
    """``asset_symbol`` and ``amount`` are None while a sweep is under way or failed."""

    tx_hash: str | None
    asset_symbol: str | None
    amount: Decimal | None
    status: str


class Refund(PayModel):
    tx_hash: str | None
    recipient: str | None
    asset_symbol: str | None
    amount: Decimal
    status: str


class StrayPayment(PayModel):
    """Money in a coin the invoice did not ask for: never counted as payment."""

    asset_symbol: str
    amount: Decimal
    sender: str | None
    tx_hash: str | None
    block_number: int
    final: bool
    swept: bool
    # automatic, receiver_settles, owner_settles or below_minimum.
    refund_handling: str


class RefundSender(PayModel):
    sender: str
    amount: Decimal


class RefundOffer(PayModel):
    available: bool
    reason: str | None
    senders: list[RefundSender]


class SellerSupportContact(PayModel):
    # email, telegram or url.
    kind: str
    display_text: str
    href: str


class MoneroInvoiceDetails(PayModel):
    confirmations: int
    confirmations_final: int


class Invoice(PayModel):
    """The account's view of an invoice (``GET /invoices/{id}``), also the ``data`` of every ``invoice.*`` event.

    Fulfil an order on ``is_paid`` (``paid`` or ``overpaid``); ``partial`` says the seller accepted a short payment."""

    id: str
    url: str
    status: str
    partial: bool
    late_payment: bool
    sweep_blocked: bool
    chain: Chain
    asset: Asset | None
    assets: list[str]
    payment_options: list[PaymentOption]
    deposit_address: str | None
    amount_due: Decimal | None
    amount_received: Decimal | None
    amount_remaining: Decimal | None
    fiat: Fiat
    quote: Quote | None
    note: str
    return_url: str | None
    success_url: str | None = None
    expires_at: datetime | None
    underpaid_open_until: datetime | None
    payment_uri: str | None
    payments: list[Payment]
    sweeps: list[Sweep]
    refunds: list[Refund]
    refund_offer: RefundOffer
    stray_payments: list[StrayPayment]
    seen: bool = False
    confirmations: int | None = None
    confirmations_final: int | None = None
    seller_support_contact: SellerSupportContact | None
    created_at: datetime
    updated_at: datetime
    destination_address: str
    order_id: str | None
    metadata: dict[str, JsonValue] | None
    accuracy_percent: Decimal
    payment_link_id: str | None
    paid_at: datetime | None
    callback_url: str | None = None
    refreshed_from_invoice_id: str | None = None
    refreshed_into_invoice_id: str | None = None
    monero: MoneroInvoiceDetails | None = None

    @property
    def is_paid(self) -> bool:
        """``paid`` or ``overpaid``: accepted and final."""
        return self.status in INVOICE_STATUSES_PAID


class InvoicePage(PayModel):
    """Newest first, 50 a page; pass ``next_cursor`` back as ``cursor`` until it is None."""

    invoices: list[Invoice]
    next_cursor: str | None


class PaymentLink(PayModel):
    """A quantity link as its account sees it. ``status`` is open, all_taken, sold_out or closed. ``lifetime_seconds``
    and ``accuracy_percent`` are None when the link was made without them: each payer's invoice then takes the
    account's default for its chain."""

    id: str
    url: str
    status: str
    quantity: int
    slots_left: int
    chain: Chain
    assets: list[str]
    payment_options: list[PaymentOption]
    fiat: Fiat
    note: str
    created_at: datetime
    destination_address: str
    lifetime_seconds: int | None
    accuracy_percent: Decimal | None
    return_url: str | None
    slots_held: int
    paid_count: int
    closed_at: datetime | None


class PaymentLinkWithInvoices(PaymentLink):
    """``GET /payment-links/{id}``: the link and its newest 200 invoices."""

    invoices: list[Invoice]


class StaticDepositPayment(PayModel):
    """One payment into a customer address. Credit your customer by ``id`` (one transfer may be several rows, or one
    row several transfers), with ``amount_usd`` of a ``credited`` one."""

    id: str
    asset_symbol: str
    amount: Decimal
    amount_usd: Decimal | None
    asset_price_usd: Decimal | None
    credited: bool
    # None when credited; wrong_coin so far.
    uncredited_reason: str | None
    tx_hash: str | None
    sender: str | None
    block_number: int
    created_at: datetime


class StaticDepositAddress(PayModel):
    """A customer's permanent deposit address. ``payments`` is filled only by ``read_static_deposit_address``."""

    id: str
    customer_reference: str
    chain: Chain
    assets: list[str]
    deposit_address: str
    destination_address: str
    sweep_blocked: bool
    received_usd: Decimal
    payment_count: int
    last_payment_at: datetime | None
    created_at: datetime
    payments: list[StaticDepositPayment]


class StaticDepositAddressPage(PayModel):
    static_deposit_addresses: list[StaticDepositAddress]
    next_cursor: str | None


class AccountEvent(PayModel):
    """One event of ``GET /events``: by API token exactly the events the webhook sends. ``data`` is the invoice (as
    ``Invoice``) for ``invoice.*`` and ``refund.*`` types and ``{"static_deposit_address", "payment" | "sweep"}`` for
    ``static_address.*`` types; ``invoice``, ``static_deposit_address`` and ``static_deposit_payment`` parse those, and
    raise ``PayEventDataError`` on an event older than a field the model needs. ``id`` is ``evt_...``, the same as the
    ``webhook-id`` the webhook sent the event with; a delivery queued before event ids took this form keeps its numeric
    ``webhook-id`` on retries and resends."""

    id: str
    type: str
    invoice_id: str | None
    created_at: datetime
    data: JsonValue

    @property
    def invoice(self) -> Invoice | None:
        return parse_event_invoice(self.type, data=self.data)

    @property
    def static_deposit_address(self) -> StaticDepositAddress | None:
        return parse_event_static_deposit_address(self.type, data=self.data)

    @property
    def static_deposit_payment(self) -> StaticDepositPayment | None:
        return parse_event_static_deposit_payment(self.type, data=self.data)


class AccountEventPage(PayModel):
    """Newest first; ``next_cursor`` reads the older page, None once there is none."""

    events: list[AccountEvent]
    next_cursor: str | None


class WebhookEvent(PayModel):
    """A verified webhook delivery (``verify_webhook``). ``webhook_id`` is the ``webhook-id`` header, the same on
    every retry and resend: store it and process each id once. ``is_test`` is True for a "Send test" delivery (the
    body's top-level ``"test": true``; its ``webhook_id`` starts ``test_`` and its sample invoice id reads 404): never
    fulfil one."""

    webhook_id: str
    type: str
    timestamp: datetime
    data: JsonValue
    is_test: bool

    @property
    def invoice(self) -> Invoice | None:
        return parse_event_invoice(self.type, data=self.data)

    @property
    def static_deposit_address(self) -> StaticDepositAddress | None:
        return parse_event_static_deposit_address(self.type, data=self.data)

    @property
    def static_deposit_payment(self) -> StaticDepositPayment | None:
        return parse_event_static_deposit_payment(self.type, data=self.data)


STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX = "static_address."


def parse_event_invoice(event_type: str, *, data: JsonValue) -> Invoice | None:
    """The invoice an ``invoice.*`` or ``refund.*`` event carries; None for any other type. ``PayEventDataError`` when
    the snapshot predates a field ``Invoice`` needs."""
    if not event_type.startswith(("invoice.", "refund.")) or not isinstance(data, dict):
        return None
    try:
        return Invoice.model_validate(data)
    except ValidationError as error:
        invoice_id = data.get("id")
        raise PayEventDataError(
            f"the {event_type} event's invoice snapshot does not parse (an event older than this SDK's Invoice "
            f"fields): call read_invoice({invoice_id!r}) for the invoice as it is now"
        ) from error


def find_event_static_deposit_address_data(event_type: str, *, data: JsonValue) -> dict[str, JsonValue] | None:
    if not event_type.startswith(STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX) or not isinstance(data, dict):
        return None
    address_data = data.get("static_deposit_address")
    return address_data if isinstance(address_data, dict) else None


def build_static_deposit_address_event_data_error(event_type: str, *, data: JsonValue) -> PayEventDataError:
    address_data = find_event_static_deposit_address_data(event_type, data=data)
    address_id = address_data.get("id") if address_data is not None else None
    return PayEventDataError(
        f"the {event_type} event's data does not parse (an event older than this SDK's models): call "
        f"read_static_deposit_address({address_id!r}) for the address and its payments as they are now"
    )


def parse_event_static_deposit_address(event_type: str, *, data: JsonValue) -> StaticDepositAddress | None:
    """The customer address a ``static_address.*`` event carries (its ``payments`` empty); None for any other type.
    ``PayEventDataError`` when the snapshot predates a field ``StaticDepositAddress`` needs."""
    address_data = find_event_static_deposit_address_data(event_type, data=data)
    if address_data is None:
        return None
    try:
        return StaticDepositAddress.model_validate(address_data)
    except ValidationError as error:
        raise build_static_deposit_address_event_data_error(event_type, data=data) from error


def parse_event_static_deposit_payment(event_type: str, *, data: JsonValue) -> StaticDepositPayment | None:
    """The payment a ``static_address.payment_received`` or ``static_address.stray_payment`` event carries; None for
    any other type (a sweep event carries ``sweep`` instead). ``PayEventDataError`` when it does not parse."""
    if not event_type.startswith(STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX) or not isinstance(data, dict):
        return None
    payment_data = data.get("payment")
    if not isinstance(payment_data, dict):
        return None
    try:
        return StaticDepositPayment.model_validate(payment_data)
    except ValidationError as error:
        raise build_static_deposit_address_event_data_error(event_type, data=data) from error
