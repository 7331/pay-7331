"""The operator key look's decision helpers (``operator_key_maintenance_service.py``): pure functions of one look's
readings, nothing read or written here."""

from app.infrastructure.safe_api.schemas import TokenAllowance
from app.payments.chains.types import AssetConfiguration, EvmChainConfiguration, TopUpRule
from app.payments.settlements.enums import OperationKind, OperatorKeyStatus
from app.payments.settlements.operator_key_assets import find_top_up_sender
from app.payments.settlements.operator_key_maintenance_types import OperatorKeyReadings
from app.payments.settlements.operator_keys import OperatorKeyAddresses, OperatorKeyStateValue
from app.payments.settlements.types import OperatorKeyFinding


def list_top_up_sending_addresses(key_addresses: OperatorKeyAddresses, *, readings: OperatorKeyReadings) -> list[str]:
    """Keys that may send a top-up this look, for themselves or for a key with no gas (module docstring of
    ``operator_key_maintenance_service.py``): active, not used elsewhere, and not backing off a failure of its own."""
    failed_sender_addresses = {
        operation.operator_address for operation in readings.recently_failed_top_ups if operation.is_sender_failure
    }
    return [
        address
        for address in key_addresses.all_addresses
        if readings.key_status_of(address) == OperatorKeyStatus.ACTIVE
        and not readings.is_used_elsewhere(address)
        and address not in failed_sender_addresses
    ]


def read_remaining_native_by_sending_address(
    chain: EvmChainConfiguration, *, readings: OperatorKeyReadings, sending_addresses: list[str]
) -> dict[str, int]:
    """The native coin each sending key holds, before the top-ups decided this look take their gas from it."""
    return {
        address: native_balance
        for address in sending_addresses
        if (native_balance := readings.balances.get(address, {}).get(chain.native_asset.symbol)) is not None
    }


def build_key_used_elsewhere_finding(
    chain: EvmChainConfiguration,
    *,
    address: str,
    state: OperatorKeyStateValue | None,
    label: str,
    remove_delegate_instruction: str,
) -> OperatorKeyFinding:
    return OperatorKeyFinding(
        problem_key=f"operator_key_used_elsewhere:{chain.key}:{state.key_id if state is not None else address}",
        alert_kind="operator_key_used_elsewhere",
        message=f"{label} on {chain.name} was used outside pay-7331 (its nonce passed what the "
        "gateway handed out): it is frozen and signs nothing here. "
        + remove_delegate_instruction
        + "Move what it holds and replace it; `operator unfreeze-key` only if that use was yours.",
        columns={"key": label, "status": "frozen", "detail": state.reason if state else ""},
    )


def find_busy_top_up_asset_symbols(address: str, *, readings: OperatorKeyReadings) -> set[str | None]:
    """The assets a key already has a top-up for: active, failed not by its sender's fault, or final in a block the
    balance read cannot show yet (still busy, module docstring of ``operator_key_maintenance_service.py``)."""
    unseen_final_top_ups = [
        operation
        for operation in readings.recently_final_top_ups
        if operation.block_number is None
        or readings.balances_block_number is None
        or operation.block_number > readings.balances_block_number
    ]
    return {
        operation.asset_symbol
        for operation in [
            *readings.active_key_operations,
            *(operation for operation in readings.recently_failed_top_ups if not operation.is_sender_failure),
            *unseen_final_top_ups,
        ]
        if operation.kind == OperationKind.TOP_UP and operation.recipient_address == address
    }


def choose_top_up_sender_address(
    address: str, *, gas_cost: int, sending_addresses: list[str], remaining_native_by_address: dict[str, int]
) -> str | None:
    """The key itself when it may send and can pay the gas, else the other sending key with the most native coin."""
    if address in sending_addresses and remaining_native_by_address.get(address, 0) >= gas_cost:
        return address
    return find_top_up_sender(
        address,
        gas_cost=gas_cost,
        remaining_native_by_address=remaining_native_by_address,
        sending_addresses=sending_addresses,
    )


def build_top_up_gas_short_finding(
    chain: EvmChainConfiguration,
    *,
    label: str,
    rule: TopUpRule,
    asset: AssetConfiguration,
    balance_text: str,
    problem_suffix: str,
) -> OperatorKeyFinding:
    return OperatorKeyFinding(
        problem_key=f"top_up_gas_short:{problem_suffix}",
        alert_kind="top_up_gas_short",
        message=f"{label} on {chain.name} holds {balance_text}, below its {rule.low_amount}, and "
        f"neither it nor another active key can pay a top-up's gas: send it "
        f"{chain.native_asset.symbol} by hand.",
        columns={"key": label, "asset": asset.symbol, "holds": balance_text, "problem": "no gas"},
    )


def build_allowance_exhausted_finding(
    chain: EvmChainConfiguration,
    *,
    label: str,
    rule: TopUpRule,
    asset: AssetConfiguration,
    allowance: TokenAllowance | None,
    balance_text: str,
    problem_suffix: str,
) -> OperatorKeyFinding:
    allowance_text = (
        "has no treasury allowance: run `treasury setup-transactions`"
        if allowance is None or allowance.amount == 0
        else "has used up its treasury allowance until its daily reset: raise it in the Safe or wait"
    )
    return OperatorKeyFinding(
        problem_key=f"allowance_exhausted:{problem_suffix}",
        alert_kind="allowance_exhausted",
        message=f"{label} on {chain.name} holds {balance_text}, below its {rule.low_amount}, and {allowance_text}.",
        columns={"key": label, "asset": asset.symbol, "holds": balance_text, "problem": "allowance"},
    )
