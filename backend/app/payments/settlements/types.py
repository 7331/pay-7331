"""What settlements hands to invoices: operations as read types and the outcomes of a watcher pass."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.payments.settlements.enums import OperationKind, OperationStatus

# A frozen key's unmined sweep, ended so another key sweeps the deposit (runner_service.py): the invoice asks for a
# new sweep instead of blocking (2026-10-13).
OPERATOR_KEY_FROZEN_FAILURE_REASON = "operator_key_frozen"
# A top-up or treasury return its key cannot sign (runner_service.py); the first three are the sending key's fault.
OPERATOR_KEY_UNUSABLE_FAILURE_REASON = "operator_key_unusable"
OPERATOR_KEY_RETIRED_FAILURE_REASON = "operator_key_retired"
OPERATOR_GAS_SHORT_FAILURE_REASON = "operator_gas_short"
TOP_UP_RECIPIENT_UNUSABLE_FAILURE_REASON = "top_up_recipient_unusable"


class OperationValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: int
    invoice_id: str
    kind: OperationKind
    status: OperationStatus
    transaction_hash: str | None
    recipient_address: str | None
    asset_symbol: str | None
    amount_base_units: int | None
    moved_base_units: int | None
    failure_reason: str | None
    created_at: datetime
    # A sweep's tokens left out by hand (``sweep-invoice --skip-token``); empty for every other operation.
    skipped_token_addresses: tuple[str, ...]

    @property
    def is_sweep_for_another_key(self) -> bool:
        """A failed sweep that only its frozen key ended: the deposit is swept again by another key, not blocked."""
        return (
            self.kind == OperationKind.SWEEP
            and self.status == OperationStatus.FAILED
            and self.failure_reason == OPERATOR_KEY_FROZEN_FAILURE_REASON
        )


class SettlementOutcome(BaseModel):
    """An operation that just reached ``final``, ``failed``, ``needs_owner`` or ``waiting_for_float``; applied to its
    invoice on the session that wrote that status, before it commits."""

    model_config = ConfigDict(frozen=True)

    operation: OperationValue


class StaticDepositSweepOutcome(BaseModel):
    """A static deposit sweep (PLAN.md section 17) that just reached ``final``, ``failed`` or ``needs_owner``; applied
    to its address on the session that wrote that status, before it commits."""

    model_config = ConfigDict(frozen=True)

    operation_id: int
    static_deposit_address_id: str
    status: OperationStatus
    transaction_hash: str | None
    failure_reason: str | None

    @property
    def is_sweep_for_another_key(self) -> bool:
        """Ended only because its key froze: the address is swept again by another key, not blocked."""
        return self.status == OperationStatus.FAILED and self.failure_reason == OPERATOR_KEY_FROZEN_FAILURE_REASON


class UnresolvedStaticDepositSweep(BaseModel):
    """A static deposit sweep still ``pending``, ``broadcast`` or ``mined``, for the stuck-operation alert."""

    model_config = ConfigDict(frozen=True)

    operation_id: int
    static_deposit_address_id: str
    status: OperationStatus
    created_at: datetime


class RefundOfferValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    invoice_id: str
    nonce: str
    sender_address: str
    asset_symbol: str
    amount_base_units: int
    message: str
    expires_at: datetime


class RefundOfferDraft(BaseModel):
    """What the invoices domain asks to offer one sender; settlements writes the text and the nonce."""

    model_config = ConfigDict(frozen=True)

    sender_address: str
    amount_base_units: int


class RefundNeedingOwner(BaseModel):
    """A refund the owner may end (``needs_owner``, ``waiting_for_float``) with what the owner looks up on an
    explorer: the nonce and every signed attempt."""

    model_config = ConfigDict(frozen=True)

    operation_id: int
    invoice_id: str
    # needs_owner or waiting_for_float
    status: str
    chain_key: str
    operator_address: str
    nonce: int | None
    recipient_address: str
    asset_symbol: str
    amount_base_units: int
    attempt_transaction_hashes: list[str]
    created_at: datetime


class OperatorKeyFinding(BaseModel):
    """Something about the operator keys or the treasury that needs the operator (the maintenance step and
    ``reconcile``). The message names a key by its ``operator_key_states`` id, role and address."""

    model_config = ConfigDict(frozen=True)

    problem_key: str
    # None: shown by ``reconcile`` only, never alerted.
    alert_kind: str | None
    message: str
    columns: dict[str, str]


class OperatorKeyRow(BaseModel):
    """One key as ``reconcile`` and ``operator list-keys`` show it."""

    model_config = ConfigDict(frozen=True)

    key_id: int | None
    operator_address: str
    role: str
    status: str
    reason: str
    configured: bool
    latest_nonce: int | None
    allocator_next_nonce: int | None
    active_operations: int
    # Asset symbol to a decimal text, "unreadable" when the read failed.
    balances: dict[str, str]
    # Asset symbol to "available / daily amount" of its Allowance Module allowance.
    allowances: dict[str, str]


class OperatorKeyReport(BaseModel):
    """What one look at a chain's operator keys and treasury found (``OperatorKeyMaintenanceService``)."""

    model_config = ConfigDict(frozen=True)

    summary: str
    rows: list[OperatorKeyRow]
    treasury_balances: dict[str, str]
    findings: list[OperatorKeyFinding]


class OperationCount(BaseModel):
    """How many of an invoice's operations of one kind are in one status (reconcile's unapplied-outcome check)."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    kind: OperationKind
    status: OperationStatus
    count: int
