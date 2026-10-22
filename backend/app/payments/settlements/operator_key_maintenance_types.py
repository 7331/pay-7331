"""The operator key look's values (``operator_key_maintenance_service.py``): what one look reads and the plan it
decides."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.infrastructure.safe_api.schemas import TokenAllowance
from app.payments.chains.types import AssetConfiguration
from app.payments.settlements.enums import OperationKind, OperationStatus, OperatorKeyRole, OperatorKeyStatus
from app.payments.settlements.operator_keys import OperatorKeyStateValue
from app.payments.settlements.types import (
    OPERATOR_GAS_SHORT_FAILURE_REASON,
    OPERATOR_KEY_FROZEN_FAILURE_REASON,
    OPERATOR_KEY_RETIRED_FAILURE_REASON,
    OPERATOR_KEY_UNUSABLE_FAILURE_REASON,
    OperatorKeyFinding,
)

# A failed top-up whose sender was at fault (unusable, retired, short of gas, or frozen while it was in flight) backs off that key as a top-up sender
# for ``TOP_UP_RETRY_AFTER_FAILURE``, not the key it fills: another key can send it on the next look (2026-10-14).
SENDER_FAILURE_REASONS = frozenset(
    {
        OPERATOR_KEY_UNUSABLE_FAILURE_REASON,
        OPERATOR_KEY_RETIRED_FAILURE_REASON,
        OPERATOR_GAS_SHORT_FAILURE_REASON,
        OPERATOR_KEY_FROZEN_FAILURE_REASON,
    }
)


class KeyOperationValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: int
    kind: OperationKind
    status: OperationStatus
    operator_address: str
    # A top-up's key it fills (operator_key_maintenance_service.py); for the other kinds whoever is paid.
    recipient_address: str | None
    asset_symbol: str | None
    failure_reason: str | None
    block_number: int | None
    created_at: datetime

    @property
    def is_sender_failure(self) -> bool:
        return self.failure_reason in SENDER_FAILURE_REASONS


class OperatorKeyReadings(BaseModel):
    """One look's reads (operator_key_maintenance_service.py)."""

    model_config = ConfigDict(frozen=True)

    block_timestamp: int
    # Wei per gas a top-up is priced at: 2 x base fee + the chain's minimum tip, under the fee cap.
    gas_price: int
    key_states: list[OperatorKeyStateValue]
    latest_nonce_by_address: dict[str, int]
    next_nonce_by_address: dict[str, int]
    active_operation_counts: dict[str, int]
    active_key_operations: list[KeyOperationValue]
    stuck_key_operations: list[KeyOperationValue]
    recently_failed_top_ups: list[KeyOperationValue]
    recently_final_top_ups: list[KeyOperationValue]
    recently_failed_treasury_returns: list[KeyOperationValue]
    final_native_return_addresses: set[str]
    # A retired key's ``eth_estimateGas`` of a native transfer to the treasury Safe, read only for one that holds native
    # coin and has no final native return; None when the node refuses to estimate it.
    native_return_estimated_gas_by_address: dict[str, int | None]
    # The block the balances were read at (Multicall3 ``getBlockNumber``); None when that call failed.
    balances_block_number: int | None
    # Address, then asset symbol, to base units; None when the read failed. Empty without a treasury.
    balances: dict[str, dict[str, int | None]]
    treasury_balances: dict[str, int | None]
    allowances: dict[str, dict[str, TokenAllowance | None]]

    def key_status_of(self, address: str) -> OperatorKeyStatus:
        """A key with no state row yet is active: it is adopted on sight."""
        state = next((state for state in self.key_states if state.operator_address == address), None)
        return state.status if state is not None else OperatorKeyStatus.ACTIVE

    def is_used_elsewhere(self, address: str) -> bool:
        """Its ``latest`` nonce is past the allocator's. A key with no state row yet (``reconcile`` before the watcher
        saw it) is adopted, not frozen, on sight."""
        latest_nonce = self.latest_nonce_by_address.get(address)
        next_nonce = self.next_nonce_by_address.get(address)
        return (
            any(state.operator_address == address for state in self.key_states)
            and latest_nonce is not None
            and next_nonce is not None
            and latest_nonce > next_nonce
        )


class TopUpDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    # The key that sends it: the key it fills, or another that pays its gas (operator_key_maintenance_service.py).
    operator_address: str
    recipient_address: str
    asset: AssetConfiguration
    amount_base_units: int


class TreasuryReturnDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    operator_address: str
    asset: AssetConfiguration
    amount_base_units: int


class KeyFreezeDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    operator_address: str
    role: OperatorKeyRole
    reason: str


class OperatorKeyPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    freezes: list[KeyFreezeDecision]
    top_ups: list[TopUpDecision]
    treasury_returns: list[TreasuryReturnDecision]
    findings: list[OperatorKeyFinding]
