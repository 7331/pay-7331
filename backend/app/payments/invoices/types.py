"""Read types and inputs other domains and the watcher use with the invoices domain."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.invoices.enums import ChainTransactionState, PaymentCheckOutcome


class NewInvoice(BaseModel):
    """An invoice to make, as the public or the account route describes it (amounts already parsed)."""

    model_config = ConfigDict(frozen=True)

    # One chain, or several EVM chains the payer picks from (PLAN.md section 15); the first is the one an old client
    # sees until the pick.
    chain_keys: list[str]
    # True when the gateway chose ``chain_keys`` (every enabled EVM chain, when the request names no network): a chain
    # with none of ``assets``, or one whose RPC cannot be reached to check the destination, is left out instead of
    # refusing the invoice.
    defaulted_chains: bool = False
    amount: Decimal
    # USD, EUR or an asset symbol.
    currency: str
    # Each chain takes the ones it lists, except a chain in ``assets_by_chain``.
    assets: list[str]
    # The maker's exact coins per chain (PLAN.md section 15): a chain named here takes this list, whatever ``assets``
    # says; its keys are among ``chain_keys``.
    assets_by_chain: dict[str, list[str]] | None = None
    # Required: the public route has the payer paste it, the account route defaults it to the payout address.
    destination_address: str
    # The signed-in account making it; None when made signed out.
    account_id: int | None
    allow_contract_destination: bool = False
    note: str = ""
    order_id: str | None = None
    lifetime_seconds: int
    accuracy_percent: Decimal
    underpaid_open_until: datetime | None = None
    return_url: str | None = None
    # The payer's link after paying, over ``return_url`` (2026-10-21).
    success_url: str | None = None
    # The invoice's own receiver of its events (an account's only, checked by ``InvoiceCallbackUrlCheck``).
    callback_url: str | None = None
    metadata: dict[str, JsonValue] | None = None
    # Set only by payments/payment_links, which counts its slots over these.
    payment_link_id: str | None = None
    # Set only by payments/invoice_schedules: the schedule and the start of the period this invoice bills; the pair is
    # unique (``ux_invoices_invoice_schedule_id_period_start``).
    invoice_schedule_id: str | None = None
    invoice_schedule_period_start: datetime | None = None
    # With ``order_id``: an expired, unpaid invoice of that order is replaced by this new one instead of returned
    # (Cryptomus ``is_refresh``; PLAN.md section 4, 2026-10-21).
    refresh_expired: bool = False


class AccountInvoiceOffer(BaseModel):
    """The chains and coins an account's invoice offers, read from a ``POST /invoices`` body (or an invoice schedule's,
    which takes the same fields): ``NewInvoice``'s ``chain_keys``, ``defaulted_chains``, ``assets`` and
    ``assets_by_chain``."""

    model_config = ConfigDict(frozen=True)

    chain_keys: list[str]
    defaulted_chains: bool
    assets: list[str]
    assets_by_chain: dict[str, list[str]] | None


# Called with (session, account id, callback URL): refuses a ``callback_url`` the account's webhooks could not send
# to, by the account webhook URL's rules (https, a public host) and with no account webhook, whose secret signs the
# callbacks. Wired by app/lifespan from payments/account_webhooks, which sits above this domain (2026-10-21). A plain
# Callable, not a Protocol: ``InvoiceWiring`` is a pydantic model, which can check only that it is callable.
InvoiceCallbackUrlCheck = Callable[[AsyncSession, int, str], Awaitable[None]]


class PaymentLinkInvoiceCounts(BaseModel):
    """One quantity link's invoices: those holding a slot and those
    paid in full or over."""

    model_config = ConfigDict(frozen=True)

    slots_held: int
    paid: int


class WatchedDeposit(BaseModel):
    """A deposit the watcher reads every tick, in the invoice's asset (and, for a token invoice, in the native coin:
    money in a coin the invoice did not ask for, INC-001)."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    deposit_address: str
    asset_symbol: str
    # None for the native coin.
    token_address: str | None
    latest_balance_base_units: int
    latest_reading_block_number: int | None
    # The credited gross and what finalized DepositSwept events moved out: a finalized rise shows through a sweep.
    received_base_units: int
    swept_base_units: int
    # A token invoice's native balance at the last reading; None for a native invoice (its own balance is that).
    latest_native_balance_base_units: int | None


class DepositBalances(BaseModel):
    """One watched deposit's balance at the finalized block and at latest."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    finalized_balance_base_units: int
    latest_balance_base_units: int
    # A token invoice's native balance at latest; None for a native invoice or when the read failed.
    latest_native_balance_base_units: int | None = None


class AttributedPayment(BaseModel):
    """A transfer into a watched deposit found in a token Transfer log or a native transaction."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    transaction_hash: str
    log_index: int
    transaction_index: int
    sender: str
    amount_base_units: int
    block_number: int
    block_hash: str
    block_timestamp: int


class AttributedStrayPayment(BaseModel):
    """Money into a live deposit in an asset its invoice did not ask for (INC-001, 2026-10-11): a token Transfer log,
    a native transaction, or (no transaction, no sender) a native rise no transaction explains. Never a payment."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    asset_symbol: str
    transaction_hash: str | None
    log_index: int
    transaction_index: int
    sender: str | None
    amount_base_units: int
    block_number: int
    block_hash: str
    block_timestamp: int


class FinalPaymentBlock(BaseModel):
    """Where a payment row ends up once its block is final (possibly moved there by a reorg)."""

    model_config = ConfigDict(frozen=True)

    block_number: int
    block_hash: str
    block_timestamp: int


class UnfinalPayment(BaseModel):
    """A payment (or stray payment) row whose block is not yet known final, with what finality needs to find its
    transfer again."""

    model_config = ConfigDict(frozen=True)

    payment_id: int
    invoice_id: str
    # The invoice's asset for a payment row, the row's own asset for a stray one.
    asset_symbol: str
    transaction_hash: str | None
    log_index: int
    sender: str | None
    amount_base_units: int
    block_number: int
    block_hash: str
    deposit_address: str
    # None for the native coin.
    token_address: str | None


class CanonicalTransactionPayments(BaseModel):
    """What one transaction pays one invoice in one asset on the canonical chain, read from its receipt once final;
    empty when the transaction is gone or reverted. It replaces every row of that transaction, invoice and asset."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    asset_symbol: str
    transaction_hash: str
    payments: list[AttributedPayment]


class RescannedDeposit(BaseModel):
    """Every configured asset's finalized balance of one deposit, from the rescan."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    balances_by_symbol: dict[str, int]


class DepositOnChain(BaseModel):
    model_config = ConfigDict(frozen=True)

    invoice_id: str
    deposit_address: str


class DepositInCreationOrder(DepositOnChain):
    """A deposit as the rescan pages through them: by its invoice's ``created_at``, then id."""

    created_at: datetime


class DepositCreationRange(BaseModel):
    """Deposits picked by when their invoice was made and whether it is closed; each rescan tier pages through one or
    more of these (2026-10-13). Every bound is optional: an empty range is every deposit of the chain."""

    model_config = ConfigDict(frozen=True)

    created_at_or_after: datetime | None = None
    created_before: datetime | None = None
    # True: closed invoices only; False: invoices not closed yet only; None: both.
    closed: bool | None = None


class DepositCreationPosition(BaseModel):
    """Where a page of ``DepositInCreationOrder`` ended: the keyset the next page starts after."""

    model_config = ConfigDict(frozen=True)

    created_at: datetime
    invoice_id: str


class TransactionTransfer(BaseModel):
    """Money one transaction moved: its native value, or one token ``Transfer`` log. ``asset_symbol`` is None for a
    token the chain's list does not have."""

    model_config = ConfigDict(frozen=True)

    recipient: str
    sender: str
    asset_symbol: str | None
    # None for the native coin.
    token_address: str | None
    # The Transfer log's index for a token; -1 for the native value.
    log_index: int
    amount_base_units: int


class ChainTransactionReading(BaseModel):
    """A transaction hash as the chain shows it (``transaction_readings.read_chain_transaction``). The block fields
    and transfers are set only for a mined, successful one; ``final`` when its block is at or below the finalized head
    and still canonical."""

    model_config = ConfigDict(frozen=True)

    transaction_hash: str
    state: ChainTransactionState
    final: bool = False
    block_number: int = 0
    block_hash: str = ""
    block_timestamp: int = 0
    transaction_index: int = 0
    finalized_block_number: int = 0
    transfers: list[TransactionTransfer] = []


class PaymentCheckResult(BaseModel):
    """What one "I paid" check found: the outcome and, where the payer's copy names them, the coin and its amount."""

    model_config = ConfigDict(frozen=True)

    outcome: PaymentCheckOutcome
    asset_symbol: str | None = None
    amount_base_units: int | None = None


class DepositRecoveryRequest(BaseModel):
    """A deposit to find again (``recover-deposit``): typed in, or one ``invoice_create`` log line (``InvoiceCreated``)
    with the deposit and destination it names."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    invoice_id: str
    chain_key: str
    destination_address: str
    # The address the log line named; None when typed in by hand.
    deposit_address: str | None = None


class RecoveredDepositBalance(BaseModel):
    """One configured asset's balance at a recovered deposit, at ``latest``."""

    model_config = ConfigDict(frozen=True)

    asset_symbol: str
    # None for the native coin.
    token_address: str | None
    decimals: int
    # None when the read failed (the asset's contract reverted).
    balance_base_units: int | None


class RecoveredDeposit(BaseModel):
    """A deposit found again from its invoice id, destination and ``PAY_INVOICE_SALT_SEED`` (``recover-deposit``)."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    chain_key: str
    factory_address: str
    destination_address: str
    deposit_address: str
    # The address the invoice's log line named, when read from a log. Another address means another seed, or an
    # invoice made before the seed (its salt was random and is only in Postgres and the backups).
    logged_deposit_address: str | None
    balances: list[RecoveredDepositBalance]
    # Shown to the operator only, to sweep by hand; kept out of reprs.
    invoice_salt: bytes = Field(repr=False)
    # ``sweepToDestination(salt, destination, tokens)``, listing every token with a balance; the salt is in it.
    sweep_token_addresses: list[str]
    sweep_call_data: bytes = Field(repr=False)

    @property
    def matches_logged_deposit_address(self) -> bool:
        return self.logged_deposit_address is None or self.logged_deposit_address == self.deposit_address


class InvoiceSettlementState(BaseModel):
    """What reconcile needs of one invoice: whether it still takes payments and how its sweep stands."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    status: str
    # Closed, or paid, overpaid, expired or cancelled: money in its deposit should be on its way out.
    payment_window_over: bool
    sweep_blocked: bool
    deposit_address: str
    asset_symbol: str | None
    received_base_units: int


class SenderLessStrayPayment(BaseModel):
    """Stray money with no sender (a contract sent it, or the watcher's block scan could not reach it): the owner
    settles it (`owner_settles`) until `record-stray-payment-from-transaction` attributes it."""

    model_config = ConfigDict(frozen=True)

    stray_payment_id: int
    invoice_id: str
    asset_symbol: str
    amount_base_units: int
    block_number: int
    final: bool


class DeferredAutomaticRefund(BaseModel):
    model_config = ConfigDict(frozen=True)

    invoice_id: str
    deferred_since: datetime
