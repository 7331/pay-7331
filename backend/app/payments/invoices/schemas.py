"""Invoice request bodies and the Invoice resource.

Amounts are decimal strings in both directions; ``accuracy_percent`` also takes a JSON number (the owner form sends
one). Times are ISO 8601 UTC.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, JsonValue, SecretStr

from app.payments.chains.schemas import ChainAsset, InvoiceChain
from app.payments.invoices.enums import (
    InvoiceStatus,
    InvoiceSweepStatus,
    PaymentCheckOutcome,
    RefundUnavailableReason,
    StrayRefundHandling,
)
from app.payments.monero_wallets.enums import MoneroNetwork
from app.payments.settlements.enums import OperationStatus
from app.shared.schemas import (
    ApiRequest,
    ApiResponse,
    IdentifierText,
    MultilineText,
    WebPageUrl,
    refuse_control_characters,
)

DEFAULT_LIFETIME_SECONDS = 30 * 60
# 5 minutes to 12 hours, as Cryptomus.
MINIMUM_LIFETIME_SECONDS = 5 * 60
MAXIMUM_LIFETIME_SECONDS = 12 * 60 * 60
ADDRESS_PATTERN = r"^0x[0-9a-fA-F]{40}$"
DECIMAL_TEXT_PATTERN = r"^[0-9]+(\.[0-9]+)?$"
# Room for the whole asset list of chains.toml (10 on Polygon, 2026-10-11) and a few more.
MAXIMUM_INVOICE_ASSETS = 16
# Every enabled EVM chain, with room for more.
MAXIMUM_OFFERED_CHAINS = 8
# The asset symbol columns are 16 characters.
AssetSymbol = Annotated[str, Field(min_length=1, max_length=16)]
# The chain key columns are 32 characters.
ChainKey = Annotated[str, Field(min_length=1, max_length=32)]
# The maker's exact coins on each chain: a chain's key and its list, at least one coin each.
AssetsByChain = Annotated[
    dict[ChainKey, Annotated[list[AssetSymbol], Field(min_length=1, max_length=MAXIMUM_INVOICE_ASSETS)]],
    Field(min_length=1, max_length=MAXIMUM_OFFERED_CHAINS),
]

# Room for an order's ids and a few short labels, near Stripe's metadata limits
# (https://docs.stripe.com/api/metadata): every key and value travels in each webhook of the invoice.
MAXIMUM_METADATA_KEYS = 50
MAXIMUM_METADATA_KEY_LENGTH = 40
MAXIMUM_METADATA_STRING_LENGTH = 500
MAXIMUM_METADATA_JSON_BYTES = 8 * 1024


def require_bounded_metadata_value(value: JsonValue) -> None:
    """Every string of the value, nested ones and the keys of nested objects too, at most
    ``MAXIMUM_METADATA_STRING_LENGTH`` characters and without NUL, which PostgreSQL's jsonb cannot hold
    (https://www.postgresql.org/docs/current/datatype-json.html)."""
    match value:
        case str():
            if len(value) > MAXIMUM_METADATA_STRING_LENGTH:
                raise ValueError(f"a metadata string is at most {MAXIMUM_METADATA_STRING_LENGTH} characters")
            if "\x00" in value:
                raise ValueError("metadata must not contain NUL characters")
        case dict():
            for nested_key, nested_value in value.items():
                require_bounded_metadata_value(nested_key)
                require_bounded_metadata_value(nested_value)
        case list():
            for item in value:
                require_bounded_metadata_value(item)
        case _:
            pass


def require_bounded_metadata(metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
    require_bounded_metadata_value(metadata)
    if len(json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode()) > MAXIMUM_METADATA_JSON_BYTES:
        raise ValueError(f"metadata is at most {MAXIMUM_METADATA_JSON_BYTES} bytes as JSON")
    return metadata


MetadataKey = Annotated[
    str, Field(min_length=1, max_length=MAXIMUM_METADATA_KEY_LENGTH), AfterValidator(refuse_control_characters)
]
# The account's own keys and values, sent back on its reads and webhooks only; never shown to the payer.
InvoiceMetadata = Annotated[
    dict[MetadataKey, JsonValue],
    Field(max_length=MAXIMUM_METADATA_KEYS),
    AfterValidator(require_bounded_metadata),
]

# A time a request sets (an underpaid invoice's last day, a schedule's start or end) lies within this of now, so a
# typo of a year never reaches the database or the period arithmetic.
MAXIMUM_REQUEST_TIME_DISTANCE = timedelta(days=5 * 366)


def require_within_request_time_distance(moment: datetime) -> datetime:
    """A time without a zone is UTC, as everywhere in the API."""
    moment_in_utc = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    if abs(moment_in_utc - datetime.now(UTC)) > MAXIMUM_REQUEST_TIME_DISTANCE:
        raise ValueError("must be within 5 years of now")
    return moment_in_utc


RequestTime = Annotated[datetime, AfterValidator(require_within_request_time_distance)]


class CreateInvoiceRequest(ApiRequest):
    amount: str = Field(pattern=DECIMAL_TEXT_PATTERN, max_length=40)
    currency: str = Field(min_length=3, max_length=16)
    # At most one of the two: ``chain`` for one chain as ever, ``chains`` for several EVM chains the payer picks from;
    # neither (nor ``assets_by_chain``) offers every enabled EVM chain.
    chain: str | None = Field(default=None, max_length=32)
    chains: list[ChainKey] | None = Field(default=None, min_length=1, max_length=MAXIMUM_OFFERED_CHAINS)
    # Every chain takes the coins of ``assets`` it lists; ``assets_by_chain`` gives a chain its own exact list. With
    # neither, a fiat invoice takes the account's saved coins on each chain and a coin amount its own coin.
    assets: list[AssetSymbol] | None = Field(default=None, min_length=1, max_length=MAXIMUM_INVOICE_ASSETS)
    assets_by_chain: AssetsByChain | None = None
    destination_address: str | None = Field(default=None, pattern=ADDRESS_PATTERN)
    allow_contract_destination: bool = False
    note: MultilineText = Field(default="", max_length=500)
    order_id: IdentifierText | None = Field(default=None, min_length=1, max_length=128)
    lifetime_seconds: int = Field(
        default=DEFAULT_LIFETIME_SECONDS, ge=MINIMUM_LIFETIME_SECONDS, le=MAXIMUM_LIFETIME_SECONDS
    )
    accuracy_percent: Decimal = Field(default=Decimal("1.0"), ge=0, le=5, max_digits=4, decimal_places=2)
    underpaid_open_until: RequestTime | None = None
    # Back to the shop (Cryptomus ``url_return``): the payer page's back button before paying, and its link after
    # paying when ``success_url`` is left out.
    return_url: WebPageUrl | None = None
    # The payer page's link once paid (Cryptomus ``url_success``).
    success_url: WebPageUrl | None = None
    # This invoice's events, signed with the account webhook's secret, beside the account webhook (Cryptomus
    # ``url_callback``). The account webhook URL's rules; 409 ``webhook_not_set`` with no account webhook.
    callback_url: str | None = Field(default=None, min_length=1, max_length=2048)
    metadata: InvoiceMetadata | None = None
    # With ``order_id``: an expired invoice of that order with no money on it is replaced by a new one (new id, new
    # deposit address, this request's terms) instead of being returned (Cryptomus ``is_refresh``).
    refresh_expired: bool = False


class CreatePublicInvoiceRequest(ApiRequest):
    """`/`'s one-tap invoice: a USD amount on every enabled EVM chain with a deposit factory (or the ``chains`` named),
    in the assets the maker ticked (every asset of each chain when ``assets`` is left out); a chain with none of the
    ticked assets is left out of the default. Signed out the receiver address is required; signed in it defaults to
    the account's payout address. No webhook, no metadata, no order id."""

    amount_usd: str = Field(pattern=DECIMAL_TEXT_PATTERN, max_length=40)
    chains: list[ChainKey] | None = Field(default=None, min_length=1, max_length=MAXIMUM_OFFERED_CHAINS)
    assets: list[AssetSymbol] | None = Field(default=None, min_length=1, max_length=MAXIMUM_INVOICE_ASSETS)
    # The exact coins per chain (the maker's ticks and saved defaults); a chain named here takes this list, any other
    # the coins of ``assets`` it lists. Without ``chains`` the chains it names are the ones offered.
    assets_by_chain: AssetsByChain | None = None
    destination_address: str | None = Field(default=None, pattern=ADDRESS_PATTERN)
    note: MultilineText = Field(default="", max_length=500)


class ChooseAssetRequest(ApiRequest):
    """The payer's pick: a coin on a chain. ``chain`` may be left out only when the coin is offered on one chain."""

    asset: AssetSymbol
    chain: str | None = Field(default=None, max_length=32)


class RefundRequestRequest(ApiRequest):
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    signature: str = Field(pattern=r"^0x[0-9a-fA-F]{130}$")


class AccountRefundRequest(ApiRequest):
    sender: str = Field(pattern=ADDRESS_PATTERN)
    amount: str = Field(pattern=DECIMAL_TEXT_PATTERN, max_length=40)


class InvoiceFiat(ApiResponse):
    """``currency`` and ``amount`` are null for a crypto invoice; ``usd`` and ``eur`` null only when no feed answers."""

    currency: str | None
    amount: str | None
    usd: str | None
    eur: str | None


class PaymentOption(ApiResponse):
    """One coin on one chain an invoice or link offers, with what it would ask now (not a quote); ``amount`` is null
    when that chain's feeds do not answer, which leaves the chain's other options and every other chain's alone. Never
    an address."""

    chain: InvoiceChain
    asset: ChainAsset
    amount: str | None


class InvoiceQuote(ApiResponse):
    asset_price_usd: str
    expires_at: datetime


class InvoicePublicPayment(ApiResponse):
    tx_hash: str | None
    sender: str | None
    amount: str
    block_number: int
    final: bool


class InvoiceSweep(ApiResponse):
    """A final sweep is one asset's DepositSwept event; a sweep under way or failed moves whatever the deposit holds,
    so its ``asset_symbol`` and ``amount`` are null."""

    tx_hash: str | None
    asset_symbol: str | None
    amount: str | None
    status: InvoiceSweepStatus


class InvoiceRefund(ApiResponse):
    tx_hash: str | None
    recipient: str | None
    # The invoice's asset, or a stray payment's when one was sent back.
    asset_symbol: str | None
    amount: str
    status: OperationStatus


class InvoicePublicStrayPayment(ApiResponse):
    """Money in an asset the invoice did not ask for, seen while it was live: never counted, swept to the receiver with
    the rest. ``sender`` and ``tx_hash`` are null when no transaction explains it (a contract sent
    it)."""

    asset_symbol: str
    amount: str
    sender: str | None
    tx_hash: str | None
    block_number: int
    final: bool
    # Everything in its asset up to and including it has reached the receiver.
    swept: bool
    # Sent back by itself, or who settles it.
    refund_handling: StrayRefundHandling


class RefundOfferSender(ApiResponse):
    sender: str
    amount: str


class InvoiceRefundOffer(ApiResponse):
    available: bool
    reason: RefundUnavailableReason | None
    senders: list[RefundOfferSender]


class InvoiceSellerSupportContact(ApiResponse):
    """How the payer reaches the seller: never the username."""

    kind: Literal["email", "telegram", "url"]
    display_text: str
    # `mailto:...`, `https://t.me/name` or an https link.
    href: str


class InvoiceMoneroDetails(ApiResponse):
    """What only a Monero invoice has; the account sees it, the payer sees the confirmations."""

    confirmations: int
    confirmations_final: int


class InvoicePublicResponse(ApiResponse):
    id: str
    url: str
    status: InvoiceStatus
    partial: bool
    late_payment: bool
    sweep_blocked: bool
    chain: InvoiceChain
    asset: ChainAsset | None
    assets: list[str]
    # Every coin on every chain still to pick from, with its estimate, while the invoice is `awaiting_asset`; empty
    # once picked. `chain` and `assets` keep describing the first offered chain until then, for clients that know one
    # chain only.
    payment_options: list[PaymentOption]
    deposit_address: str | None
    amount_due: str | None
    amount_received: str | None
    amount_remaining: str | None
    fiat: InvoiceFiat
    quote: InvoiceQuote | None
    note: str
    return_url: str | None
    # The link after paying; the page falls back to ``return_url`` when null.
    success_url: str | None
    expires_at: datetime | None
    underpaid_open_until: datetime | None
    payment_uri: str | None
    payments: list[InvoicePublicPayment]
    sweeps: list[InvoiceSweep]
    refunds: list[InvoiceRefund]
    refund_offer: InvoiceRefundOffer
    stray_payments: list[InvoicePublicStrayPayment]
    # A Monero invoice with a pool transaction paying its subaddress, not in a block yet: the payer page says "Seen".
    # Never credited; false on every EVM invoice.
    seen: bool
    # A Monero invoice's confirmations of its best payment (its newest credited one) and the count at which money
    # moves; null on every EVM invoice and on a Monero one with nothing credited yet.
    confirmations: int | None
    confirmations_final: int | None
    # The invoice's account's support contact; null for a signed-out invoice or an account that set none.
    seller_support_contact: InvoiceSellerSupportContact | None
    created_at: datetime
    updated_at: datetime


class InvoiceAccountResponse(InvoicePublicResponse):
    destination_address: str
    order_id: str | None
    metadata: dict[str, JsonValue] | None
    accuracy_percent: str
    # The quantity link a payer made it from; null for every other invoice.
    payment_link_id: str | None
    # The invoice schedule that made it and the start of the period it bills; null for every other invoice.
    invoice_schedule_id: str | None
    invoice_schedule_period_start: datetime | None
    # When it became paid or overpaid, or was accepted as partially paid; null before that.
    paid_at: datetime | None
    # Its own receiver of its events, if it was made with one.
    callback_url: str | None
    # The expired invoice of the same order this one replaced, and the one that replaced this.
    refreshed_from_invoice_id: str | None
    refreshed_into_invoice_id: str | None
    # Null on every EVM invoice.
    monero: InvoiceMoneroDetails | None


class InvoicePageResponse(ApiResponse):
    invoices: list[InvoiceAccountResponse]
    next_cursor: str | None


class RefundOfferItemResponse(ApiResponse):
    sender: str
    amount: str
    asset: str
    nonce: str
    message: str
    expires_at: datetime


class RefundOfferListResponse(ApiResponse):
    offers: list[RefundOfferItemResponse]


class PaymentCheckRequest(ApiRequest):
    """The payer's "I paid": without ``transaction_hash`` the deposit is reread; with it, that
    transaction is looked up. Any text up to 200 characters: one that is not an EVM hash is the outcome
    ``hash_malformed``, not a refusal, so the page can say what it looks like instead."""

    transaction_hash: str | None = Field(default=None, max_length=200)


class PaymentCheckResponse(ApiResponse):
    """What the check found; the coin and amount the copy names (null where it names none); the invoice as it now
    stands."""

    outcome: PaymentCheckOutcome
    asset_symbol: str | None
    amount: str | None
    invoice: InvoicePublicResponse


# A private view key is 32 bytes, 64 hex characters (https://docs.getmonero.org/cryptography/asymmetric/private-key/).
PRIVATE_VIEW_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def require_private_view_key_hex(private_view_key: SecretStr) -> SecretStr:
    """Checked here, so the sidecar only ever sees a key of the right shape; the refusal never quotes the key."""
    if not PRIVATE_VIEW_KEY_PATTERN.fullmatch(private_view_key.get_secret_value()):
        raise ValueError("must be 64 hex characters")
    return private_view_key


class MoneroChainSummary(ApiResponse):
    """An enabled Monero chain an account can set a wallet on."""

    key: str
    name: str
    network: MoneroNetwork


class MoneroWallet(ApiResponse):
    """The account's active wallet of one Monero chain; never the view key."""

    chain_key: str
    primary_address: str
    network: MoneroNetwork
    confirmed_lookahead_minor_count: int
    # Allocated minors past the highest one with an output seen in a block.
    unreceived_minor_gap: int
    # True once the gap reached the warning (the confirmed lookahead less 50): `/activity` shows the widening line.
    lookahead_warning: bool
    created_at: datetime


class MoneroWalletSettingsResponse(ApiResponse):
    """``GET`` and ``PUT /account-monero-wallet`` and ``POST /account-monero-wallet/lookahead-confirmations``."""

    monero_chains: list[MoneroChainSummary]
    wallet: MoneroWallet | None


class MoneroWalletChangeOptionsRequest(ApiRequest):
    """``POST /account-monero-wallet/change-options``: the wallet the passkey check approves; the ``PUT`` that carries
    the assertion must name the same one."""

    chain_key: str = Field(min_length=1, max_length=32)
    primary_address: str = Field(min_length=1, max_length=106)


class SetMoneroWalletRequest(ApiRequest):
    """``PUT /account-monero-wallet``: the wallet and ``credential.toJSON()`` of the passkey check from ``POST
    /account-monero-wallet/change-options``."""

    chain_key: str = Field(min_length=1, max_length=32)
    # A standard primary address is 95 base58 characters. Only bounded here: the sidecar judges length, checksum and
    # network, so every wrong address is one refusal, `monero_address_invalid`.
    primary_address: str = Field(min_length=1, max_length=106)
    private_view_key: Annotated[SecretStr, AfterValidator(require_private_view_key_hex)]
    credential: dict[str, JsonValue]


class ConfirmMoneroLookaheadRequest(ApiRequest):
    """``POST /account-monero-wallet/lookahead-confirmations``: the receiver ran ``set subaddress-lookahead 50:1000``
    in this chain's wallet."""

    chain_key: str = Field(min_length=1, max_length=32)
