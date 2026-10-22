"""OperatorKeyMaintenanceService: the watcher's once-a-minute look at one chain's operator keys and treasury
(2026-10-13; PLAN.md section 5, operator keys and treasury). ``reconcile`` runs the same reads and decisions without
writing anything.

Reads, bounded by the configured keys and top-up assets, never by deposits or operations:
one JSON-RPC batch of every configured key's nonce at ``latest``, then the key rows, nonce rows and key operations
from Postgres, then, with a treasury, one Multicall3 ``aggregate3`` holding its block number, every key's balance in
each top-up asset (and the native coin), the Safe's balance in each, and every key's ``getTokenAllowance`` per asset
of its role. The
chain's nonces are read before Postgres's nonce rows, so a nonce the runner handed out is always in the row read
after it.

Decisions:

- Leak: a key whose ``latest`` nonce is past its ``operator_nonces.next_nonce`` was used outside this system: frozen
  (``operator_key_service.py``) and alerted ``operator_key_used_elsewhere`` (again each hour while frozen). The alert
  tells the owner to remove the key's Allowance Module delegate at once: ``executeAllowanceTransfer`` checks only the
  allowance, not where it pays, so a leaked delegate can still pull a day's allowance from the Safe to any address.
  A key seen for the first time is adopted first, at the highest ``latest`` count of every endpoint. A ``retired`` key
  is never frozen or alerted: the owner may move what it holds by hand, and the runner adopts its nonce for its returns.
- Top-up (an active key, treasury on): for each top-up rule of the key's role whose asset the key holds below
  ``low_amount``, with no active top-up of that key and asset, none failed in the last ``TOP_UP_RETRY_AFTER_FAILURE``
  unless its sender was at fault, and none final in the last ``TOP_UP_SETTLE_WINDOW`` in a block the balance read does
  not include yet (after a failover to a lagging endpoint the balance can predate a top-up that already went final): a
  ``top_up`` operation of min(target - balance, what the allowance has left this period, the Safe's balance). What the
  allowance has left accounts for its daily reset: the module zeroes ``spent`` once ``resetTimeMin`` minutes passed
  since ``lastResetMin``. Nothing left (or no allowance set): ``allowance_exhausted``; an empty Safe is
  ``treasury_low``'s. A key whose native balance cannot pay ``TOP_UP_GAS_ESTIMATE`` gas at the current fee has its
  top-up sent by the active key with the most native coin that can (2026-10-14: a new sweeper starts empty), carrying
  the needy key's delegate signature (``operation_transactions.py``): the money still goes only from the Safe to the
  needy key. With no such key it asks the owner instead (``top_up_gas_short``). Each top-up decided takes its gas from
  its sender's balance for the rest of the look, so one key is not handed more top-ups than it can pay for. A top-up's
  ``recipient_address`` is the key it fills, its ``operator_address`` the key that sends it; "a key's top-ups" always
  means its recipient's. A failure of the sender's own (``SENDER_FAILURE_REASONS``,
  ``operator_key_maintenance_types.py``) backs off that sender, not the key it fills.
- Treasury low: the Safe holds less of an asset than one day of every active key's allowance in it
  (``treasury_low``).
- Retired key (treasury on), once it has no active operation: a ``treasury_return`` of each top-up token it holds,
  then, with no token left, one of its native coin less a gas reserve sized from the node's estimate of that transfer
  (``treasury_return_gas_reserve_of``: twice the gas limit it asks for, at the fee cap; a rollup's estimate includes the
  parent chain's posting fee and moves with it). The amount is fixed when the return is made.
  A return that failed before or at signing waits ``TOP_UP_RETRY_AFTER_FAILURE`` for that key and asset, and alerts
  ``treasury_return_failed`` (at most hourly per key and asset, the alert recorder's rule).
- Key operations unresolved for ``KEY_OPERATION_STUCK_AGE`` alert ``operation_stuck``; a failed top-up alerts
  ``top_up_failed``. Both name the key a top-up fills and, when another key sends it, that sender.

The runner sends what this records on the same tick, through its usual machinery (estimate first, the nonce
allocator, stored before broadcast, bumps, finality).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import time

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import (
    EvmRpcClient,
    EvmRpcEndpoint,
    decode_uint256,
    encode_block_number_call,
)
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, Multicall3Result
from app.infrastructure.safe_api.client import decode_token_allowance, encode_get_token_allowance_call
from app.infrastructure.safe_api.schemas import TokenAllowance
from app.payments.chains.types import AssetConfiguration, EvmChainConfiguration
from app.payments.settlements.enums import OperationKind, OperationStatus, OperatorKeyRole, OperatorKeyStatus
from app.payments.settlements.log_events import KeyOperationRecorded
from app.payments.settlements.models import OutgoingOperation
from app.payments.settlements.operation_transactions import (
    read_native_return_gas_estimates,
    treasury_return_gas_reserve_of,
)
from app.payments.settlements.operator_key_assets import balance_call_of, top_up_assets_of
from app.payments.settlements.operator_key_maintenance_decisions import (
    build_allowance_exhausted_finding,
    build_key_used_elsewhere_finding,
    build_top_up_gas_short_finding,
    choose_top_up_sender_address,
    find_busy_top_up_asset_symbols,
    list_top_up_sending_addresses,
    read_remaining_native_by_sending_address,
)
from app.payments.settlements.operator_key_maintenance_types import (
    KeyFreezeDecision,
    KeyOperationValue,
    OperatorKeyPlan,
    OperatorKeyReadings,
    TopUpDecision,
    TreasuryReturnDecision,
)
from app.payments.settlements.operator_key_service import OperatorKeyService
from app.payments.settlements.operator_keys import OperatorKeyAddresses
from app.payments.settlements.repository import ACTIVE_STATUSES, SettlementRepository
from app.payments.settlements.types import OperatorKeyFinding, OperatorKeyReport, OperatorKeyRow
from app.shared import logging as log
from app.shared.amounts import format_base_units, format_base_units_for_reading

logger = log.get_logger(__name__)

MAINTENANCE_INTERVAL_SECONDS = 60
# executeAllowanceTransfer of a token through a Safe module: about 90k gas; headroom for a cold Safe.
TOP_UP_GAS_ESTIMATE = 150_000
KEY_OPERATION_STUCK_AGE = timedelta(minutes=30)
TOP_UP_RETRY_AFTER_FAILURE = timedelta(minutes=30)
TOP_UP_SETTLE_WINDOW = timedelta(minutes=5)

type SessionFactory = Callable[[], AsyncSession]


def decode_uint256_result_or_none(result: Multicall3Result) -> int | None:
    """A uint256 an aggregate3 call returned; None when that call failed."""
    return decode_uint256(result.return_data) if result.succeeded else None


def key_operation_value_of(operation: OutgoingOperation) -> KeyOperationValue:
    return KeyOperationValue(
        operation_id=operation.id,
        kind=operation.kind,
        status=operation.status,
        operator_address=operation.operator_address,
        recipient_address=operation.recipient_address,
        asset_symbol=operation.asset_symbol,
        failure_reason=operation.failure_reason,
        block_number=operation.block_number,
        created_at=operation.created_at,
    )


class OperatorKeyMaintenanceService:
    def __init__(
        self,
        *,
        chain: EvmChainConfiguration,
        key_addresses: OperatorKeyAddresses,
        session_factory: SessionFactory,
        rpc_client: EvmRpcClient,
    ) -> None:
        self.chain = chain
        self.key_addresses = key_addresses
        self.session_factory = session_factory
        # Every endpoint of the chain: a new key is adopted at the highest ``latest`` count any of them answers.
        self.rpc_client = rpc_client
        self.last_run_monotonic: float | None = None

    async def run_when_due(self, endpoint: EvmRpcEndpoint, *, block_heads: BlockHeads) -> list[OperatorKeyFinding]:
        """At most once per ``MAINTENANCE_INTERVAL_SECONDS``: read, decide, record; the findings to alert."""
        now = time.monotonic()
        if self.last_run_monotonic is not None and now - self.last_run_monotonic < MAINTENANCE_INTERVAL_SECONDS:
            return []
        self.last_run_monotonic = now
        readings = await self.read_readings(endpoint, block_heads=block_heads, record_new_keys=True)
        plan = self.decide(readings)
        await self.apply(plan)
        return plan.findings

    async def build_report(self, endpoint: EvmRpcEndpoint, *, block_heads: BlockHeads) -> OperatorKeyReport:
        """``reconcile``: the same reads and decisions, nothing written."""
        readings = await self.read_readings(endpoint, block_heads=block_heads, record_new_keys=False)
        return self.report_of(readings, plan=self.decide(readings))

    # Reads

    async def read_readings(
        self, endpoint: EvmRpcEndpoint, *, block_heads: BlockHeads, record_new_keys: bool
    ) -> OperatorKeyReadings:
        chain = self.chain
        configured_addresses = self.key_addresses.all_addresses
        # The chain's nonces first, Postgres's after (module docstring).
        latest_nonces = await endpoint.read_transaction_counts(configured_addresses, block_tag="latest")
        latest_nonce_by_address = dict(zip(configured_addresses, latest_nonces, strict=True))
        now = datetime.now(UTC)
        if record_new_keys:
            latest_nonce_by_address = await self.raise_new_keys_to_highest_latest_nonce(latest_nonce_by_address)
            async with self.session_factory() as session, session.begin():
                await OperatorKeyService.on_session(session).ensure_key_states(
                    chain.key, addresses=self.key_addresses, latest_nonce_by_address=latest_nonce_by_address
                )
        async with self.session_factory() as session:
            repository = SettlementRepository(session=session)
            key_states = await OperatorKeyService(repository=repository).list_key_states(chain.key)
            next_nonce_by_address = await repository.read_next_nonces(
                chain.key, operator_addresses=configured_addresses
            )
            active_operation_counts = await repository.count_active_operations_by_operator(chain.key)
            active_key_operations = await repository.list_key_operations(chain.key, statuses=ACTIVE_STATUSES)
            recently_failed_key_operations = await repository.list_key_operations(
                chain.key, statuses=[OperationStatus.FAILED], updated_since=now - TOP_UP_RETRY_AFTER_FAILURE
            )
            recently_final_top_ups = [
                operation
                for operation in await repository.list_key_operations(
                    chain.key, statuses=[OperationStatus.FINAL], updated_since=now - TOP_UP_SETTLE_WINDOW
                )
                if operation.kind == OperationKind.TOP_UP
            ]
            final_native_return_addresses = await repository.list_final_native_treasury_return_addresses(chain.key)
        fee_cap = chain.maximum_fee_per_gas_wei
        gas_price = min(2 * block_heads.latest.base_fee_per_gas + chain.minimum_priority_fee_per_gas_wei, fee_cap)
        balances_block_number, balances, treasury_balances, allowances = await self.read_treasury_balances(endpoint)
        native_symbol = chain.native_asset.symbol
        native_return_estimated_gas_by_address = await read_native_return_gas_estimates(
            endpoint,
            chain=chain,
            key_addresses=[
                state.operator_address
                for state in key_states
                if state.status == OperatorKeyStatus.RETIRED
                and state.operator_address not in final_native_return_addresses
                and (balances.get(state.operator_address, {}).get(native_symbol) or 0) > 0
            ],
        )
        return OperatorKeyReadings(
            block_timestamp=block_heads.latest.timestamp,
            gas_price=gas_price,
            key_states=key_states,
            latest_nonce_by_address=latest_nonce_by_address,
            next_nonce_by_address=next_nonce_by_address,
            active_operation_counts=active_operation_counts,
            active_key_operations=[key_operation_value_of(operation) for operation in active_key_operations],
            stuck_key_operations=[
                key_operation_value_of(operation)
                for operation in active_key_operations
                if operation.created_at < now - KEY_OPERATION_STUCK_AGE
            ],
            recently_failed_top_ups=[
                key_operation_value_of(operation)
                for operation in recently_failed_key_operations
                if operation.kind == OperationKind.TOP_UP
            ],
            recently_final_top_ups=[key_operation_value_of(operation) for operation in recently_final_top_ups],
            recently_failed_treasury_returns=[
                key_operation_value_of(operation)
                for operation in recently_failed_key_operations
                if operation.kind == OperationKind.TREASURY_RETURN
            ],
            final_native_return_addresses=final_native_return_addresses,
            native_return_estimated_gas_by_address=native_return_estimated_gas_by_address,
            balances_block_number=balances_block_number,
            balances=balances,
            treasury_balances=treasury_balances,
            allowances=allowances,
        )

    async def raise_new_keys_to_highest_latest_nonce(self, latest_nonce_by_address: dict[str, int]) -> dict[str, int]:
        """A key with no state row yet is adopted at its nonce (module docstring), once: at the highest ``latest``
        count of every endpoint, since the tick's one may lag and its lower count would later read as a leak."""
        async with self.session_factory() as session:
            recorded_addresses = {
                key_state.operator_address
                for key_state in await SettlementRepository(session=session).list_operator_key_states(self.chain.key)
            }
        new_addresses = [address for address in self.key_addresses.all_addresses if address not in recorded_addresses]
        if not new_addresses:
            return latest_nonce_by_address
        highest_counts = await self.rpc_client.read_highest_transaction_counts(new_addresses, block_tag="latest")
        return latest_nonce_by_address | {
            address: max(count, latest_nonce_by_address.get(address, 0))
            for address, count in zip(new_addresses, highest_counts, strict=True)
        }

    async def read_treasury_balances(
        self, endpoint: EvmRpcEndpoint
    ) -> tuple[
        int | None,
        dict[str, dict[str, int | None]],
        dict[str, int | None],
        dict[str, dict[str, TokenAllowance | None]],
    ]:
        """One aggregate3 at ``latest``: its block number, key balances, the Safe's balances and the keys'
        allowances. Without a treasury only the keys' native balances (their gas)."""
        chain = self.chain
        treasury = chain.treasury
        addresses = self.key_addresses.all_addresses
        assets = top_up_assets_of(chain) if treasury.is_enabled else [chain.native_asset]
        calls = [
            encode_block_number_call(),
            *(balance_call_of(asset, holder_address=address) for address in addresses for asset in assets),
        ]
        allowance_keys: list[tuple[str, AssetConfiguration]] = []
        safe_address = treasury.treasury_safe_address
        module_address = treasury.allowance_module_address
        if safe_address is not None and module_address is not None:
            calls += [balance_call_of(asset, holder_address=safe_address) for asset in assets]
            allowance_keys = self.list_top_up_allowance_keys()
            calls += [
                encode_get_token_allowance_call(
                    allowance_module_address=module_address,
                    safe_address=safe_address,
                    delegate_address=address,
                    token_address=asset.token_address,
                )
                for address, asset in allowance_keys
            ]
        results = await endpoint.aggregate3(calls, block_hash=None)
        balances_block_number = decode_uint256_result_or_none(results[0])
        balances: dict[str, dict[str, int | None]] = {}
        position = 1
        for address in addresses:
            for asset in assets:
                balances.setdefault(address, {})[asset.symbol] = decode_uint256_result_or_none(results[position])
                position += 1
        treasury_balances: dict[str, int | None] = {}
        allowances: dict[str, dict[str, TokenAllowance | None]] = {}
        if safe_address is not None and module_address is not None:
            for asset in assets:
                treasury_balances[asset.symbol] = decode_uint256_result_or_none(results[position])
                position += 1
            for address, asset in allowance_keys:
                allowances.setdefault(address, {})[asset.symbol] = decode_token_allowance(results[position])
                position += 1
        return balances_block_number, balances, treasury_balances, allowances

    def list_top_up_allowance_keys(self) -> list[tuple[str, AssetConfiguration]]:
        """Each configured key and each asset of its role's top-up rules: the allowances the look reads."""
        chain = self.chain
        allowance_keys: list[tuple[str, AssetConfiguration]] = []
        for address in self.key_addresses.all_addresses:
            role = self.key_addresses.role_of(address)
            for rule in chain.treasury.top_up_rules_of_role(role.value if role is not None else ""):
                asset = chain.find_asset(rule.asset_symbol)
                if asset is not None:
                    allowance_keys.append((address, asset))
        return allowance_keys

    # Decisions: reads in, a plan out, nothing written

    def label_of(self, address: str, *, readings: OperatorKeyReadings) -> str:
        """How alerts name a key: its row id, role and address, so the operator can send to it."""
        state = next((state for state in readings.key_states if state.operator_address == address), None)
        role = self.key_addresses.role_of(address) or (state.role if state is not None else None)
        role_text = role.value if role is not None else "unconfigured"
        return (
            f"Operator key {state.key_id} ({role_text}, {address})"
            if state is not None
            else f"A new {role_text} key ({address})"
        )

    def decide(self, readings: OperatorKeyReadings) -> OperatorKeyPlan:
        chain = self.chain
        state_by_address = {state.operator_address: state for state in readings.key_states}
        freezes: list[KeyFreezeDecision] = []
        top_ups: list[TopUpDecision] = []
        treasury_returns: list[TreasuryReturnDecision] = []
        findings: list[OperatorKeyFinding] = []
        # Keys that may send a top-up this look, for themselves or for a key with no gas (module docstring), and the
        # native coin each has left once the top-ups already decided this look paid their gas.
        sending_addresses = list_top_up_sending_addresses(self.key_addresses, readings=readings)
        remaining_native_by_address = read_remaining_native_by_sending_address(
            chain, readings=readings, sending_addresses=sending_addresses
        )
        for address in self.key_addresses.all_addresses:
            state = state_by_address.get(address)
            status = readings.key_status_of(address)
            role = self.key_addresses.role_of(address) or OperatorKeyRole.SWEEPER
            label = self.label_of(address, readings=readings)
            if status == OperatorKeyStatus.RETIRED:
                # Never frozen: its owner may empty it by hand; only its returns to the Safe still sign (runner).
                if chain.treasury.is_enabled:
                    treasury_returns.extend(self.decide_treasury_returns(address, readings=readings))
                continue
            latest_nonce = readings.latest_nonce_by_address.get(address)
            next_nonce = readings.next_nonce_by_address.get(address)
            used_elsewhere = readings.is_used_elsewhere(address)
            if status == OperatorKeyStatus.FROZEN or used_elsewhere:
                if status != OperatorKeyStatus.FROZEN:
                    freezes.append(
                        KeyFreezeDecision(
                            operator_address=address,
                            role=role,
                            reason=f"used elsewhere: latest nonce {latest_nonce}, allocator next {next_nonce}",
                        )
                    )
                findings.append(
                    build_key_used_elsewhere_finding(
                        chain,
                        address=address,
                        state=state,
                        label=label,
                        remove_delegate_instruction=self.remove_delegate_instruction(),
                    )
                )
                continue
            if chain.treasury.is_enabled and status == OperatorKeyStatus.ACTIVE:
                self.decide_top_ups(
                    address,
                    role=role,
                    label=label,
                    readings=readings,
                    sending_addresses=sending_addresses,
                    remaining_native_by_address=remaining_native_by_address,
                    top_ups=top_ups,
                    findings=findings,
                )
        findings.extend(self.find_treasury_low(readings))
        findings.extend(self.find_key_operation_problems(readings))
        return OperatorKeyPlan(freezes=freezes, top_ups=top_ups, treasury_returns=treasury_returns, findings=findings)

    def remove_delegate_instruction(self) -> str:
        """The freeze alert's first ask (module docstring); nothing without a treasury Safe."""
        chain = self.chain
        if not chain.treasury.is_enabled:
            return ""
        return (
            "Its treasury allowance still lets it pull up to a day's allowance from the Safe to any address: remove "
            f"its delegate NOW: `docker exec pay-7331-watcher python -m app.cli treasury setup-transactions --chain "
            f"{chain.key} > removal.json` lists `removeDelegate` for it; load that file in the Safe app's Transaction "
            "Builder and sign it. "
        )

    def decide_top_ups(
        self,
        address: str,
        *,
        role: OperatorKeyRole,
        label: str,
        readings: OperatorKeyReadings,
        sending_addresses: list[str],
        remaining_native_by_address: dict[str, int],
        top_ups: list[TopUpDecision],
        findings: list[OperatorKeyFinding],
    ) -> None:
        chain = self.chain
        balances = readings.balances.get(address, {})
        busy_symbols = find_busy_top_up_asset_symbols(address, readings=readings)
        for rule in chain.treasury.top_up_rules_of_role(role.value):
            asset = chain.find_asset(rule.asset_symbol)
            balance = balances.get(rule.asset_symbol)
            if asset is None or balance is None or balance >= asset.base_units_of(rule.low_amount):
                continue
            if rule.asset_symbol in busy_symbols:
                continue
            key_state = next((state for state in readings.key_states if state.operator_address == address), None)
            key_reference = key_state.key_id if key_state is not None else "new"
            problem_suffix = f"{chain.key}:{key_reference}:{asset.symbol}"
            balance_text = f"{format_base_units(balance, decimals=asset.decimals)} {asset.symbol}"
            gas_cost = TOP_UP_GAS_ESTIMATE * readings.gas_price
            sender_address = choose_top_up_sender_address(
                address,
                gas_cost=gas_cost,
                sending_addresses=sending_addresses,
                remaining_native_by_address=remaining_native_by_address,
            )
            if sender_address is None:
                findings.append(
                    build_top_up_gas_short_finding(
                        chain,
                        label=label,
                        rule=rule,
                        asset=asset,
                        balance_text=balance_text,
                        problem_suffix=problem_suffix,
                    )
                )
                continue
            allowance = readings.allowances.get(address, {}).get(asset.symbol)
            available = allowance.available_at(block_timestamp=readings.block_timestamp) if allowance else 0
            treasury_balance = readings.treasury_balances.get(asset.symbol) or 0
            if available <= 0:
                findings.append(
                    build_allowance_exhausted_finding(
                        chain,
                        label=label,
                        rule=rule,
                        asset=asset,
                        allowance=allowance,
                        balance_text=balance_text,
                        problem_suffix=problem_suffix,
                    )
                )
                continue
            amount = min(asset.base_units_of(rule.target_amount) - balance, available, treasury_balance)
            # An empty Safe is ``treasury_low``'s alert.
            if amount <= 0:
                continue
            remaining_native_by_address[sender_address] -= gas_cost
            top_ups.append(
                TopUpDecision(
                    operator_address=sender_address, recipient_address=address, asset=asset, amount_base_units=amount
                )
            )

    def decide_treasury_returns(self, address: str, *, readings: OperatorKeyReadings) -> list[TreasuryReturnDecision]:
        """A retired key with no active operation, nor a top-up another key still sends it: its tokens first, then its
        native coin once (a top-up landing after that would stay on the key)."""
        chain = self.chain
        if readings.active_operation_counts.get(address, 0) > 0 or any(
            operation.kind == OperationKind.TOP_UP and operation.recipient_address == address
            for operation in readings.active_key_operations
        ):
            return []
        backing_off_symbols = {
            operation.asset_symbol
            for operation in readings.recently_failed_treasury_returns
            if operation.operator_address == address
        }
        if backing_off_symbols:
            # One failed: the next try waits, tokens and native coin alike (the native return comes last anyway).
            return []
        balances = readings.balances.get(address, {})
        returns = [
            TreasuryReturnDecision(operator_address=address, asset=asset, amount_base_units=balance)
            for asset in top_up_assets_of(chain)
            if asset.token_address is not None and (balance := balances.get(asset.symbol))
        ]
        if returns or any(balances.get(asset.symbol) is None for asset in top_up_assets_of(chain)):
            return returns
        if address in readings.final_native_return_addresses:
            return []
        native_balance = balances.get(chain.native_asset.symbol) or 0
        reserve = treasury_return_gas_reserve_of(
            chain, estimated_gas=readings.native_return_estimated_gas_by_address.get(address)
        )
        if native_balance <= reserve:
            return []
        return [
            TreasuryReturnDecision(
                operator_address=address, asset=chain.native_asset, amount_base_units=native_balance - reserve
            )
        ]

    def find_treasury_low(self, readings: OperatorKeyReadings) -> list[OperatorKeyFinding]:
        chain = self.chain
        if not chain.treasury.is_enabled:
            return []
        active_addresses = [
            address
            for address in self.key_addresses.all_addresses
            if all(
                state.status == OperatorKeyStatus.ACTIVE
                for state in readings.key_states
                if state.operator_address == address
            )
        ]
        findings: list[OperatorKeyFinding] = []
        for asset in top_up_assets_of(chain):
            needed = sum(
                asset.base_units_of(rule.daily_allowance_amount)
                for address in active_addresses
                for rule in chain.treasury.top_up_rules_of_role(
                    (self.key_addresses.role_of(address) or OperatorKeyRole.SWEEPER).value
                )
                if rule.asset_symbol == asset.symbol
            )
            held = readings.treasury_balances.get(asset.symbol)
            if needed == 0 or (held is not None and held >= needed):
                continue
            held_text = "unreadable" if held is None else format_base_units(held, decimals=asset.decimals)
            held_reading_text = (
                "an unreadable amount of"
                if held is None
                else format_base_units_for_reading(held, decimals=asset.decimals)
            )
            needed_text = format_base_units(needed, decimals=asset.decimals)
            findings.append(
                OperatorKeyFinding(
                    problem_key=f"treasury_low:{chain.key}:{asset.symbol}",
                    alert_kind="treasury_low",
                    message=f"The {chain.name} treasury Safe {chain.treasury.treasury_safe_address}, which tops up pay-7331's "
                    "operator keys, holds "
                    f"{held_reading_text} {asset.symbol}, less than one day of the keys' allowances ({needed_text} "
                    f"{asset.symbol}): once it is empty the keys stop being topped up. Send {asset.symbol} on "
                    f"{chain.name} to the Safe.",
                    columns={"key": "treasury Safe", "asset": asset.symbol, "holds": held_text, "problem": "low"},
                )
            )
        return findings

    def keys_text_of(self, operation: KeyOperationValue, *, readings: OperatorKeyReadings) -> str:
        """The key an operation is for, and for a top-up another key sends, that sender (module docstring)."""
        operator_label = self.label_of(operation.operator_address, readings=readings).lower()
        if operation.kind != OperationKind.TOP_UP or operation.recipient_address in {None, operation.operator_address}:
            return operator_label
        return (
            f"{self.label_of(operation.recipient_address or '', readings=readings).lower()}, sent by {operator_label}"
        )

    def find_key_operation_problems(self, readings: OperatorKeyReadings) -> list[OperatorKeyFinding]:
        chain = self.chain
        retry_minutes = int(TOP_UP_RETRY_AFTER_FAILURE.total_seconds() // 60)
        findings = [
            OperatorKeyFinding(
                problem_key=f"operation_stuck:{operation.operation_id}",
                alert_kind="operation_stuck",
                message=f"{operation.kind.value.capitalize()} {operation.operation_id} of "
                f"{self.keys_text_of(operation, readings=readings)} on {chain.name} is still "
                f"{operation.status.value} after {int(KEY_OPERATION_STUCK_AGE.total_seconds() // 60)} minutes.",
                columns={
                    "key": str(operation.operation_id),
                    "asset": operation.asset_symbol or "",
                    "holds": "",
                    "problem": f"{operation.kind.value} {operation.status.value}",
                },
            )
            for operation in readings.stuck_key_operations
        ]
        findings.extend(
            OperatorKeyFinding(
                problem_key=f"top_up_failed:{operation.operation_id}",
                alert_kind="top_up_failed",
                message=f"A {operation.asset_symbol} top-up of {self.keys_text_of(operation, readings=readings)} on "
                f"{chain.name} failed ({operation.failure_reason}); "
                + (
                    f"its sender sends no top-up for {retry_minutes} minutes, another key may send it now. "
                    "Check that key's gas and status with `operator list-keys`."
                    if operation.is_sender_failure
                    else f"the next try waits {retry_minutes} minutes. Check the treasury setup with `treasury status`."
                ),
                columns={
                    "key": str(operation.operation_id),
                    "asset": operation.asset_symbol or "",
                    "holds": "",
                    "problem": f"top_up failed: {operation.failure_reason}",
                },
            )
            for operation in readings.recently_failed_top_ups
        )
        findings.extend(
            OperatorKeyFinding(
                problem_key=f"treasury_return_failed:{chain.key}:"
                f"{self.key_reference_of(operation.operator_address, readings=readings)}:{operation.asset_symbol}",
                alert_kind="treasury_return_failed",
                message=f"A {operation.asset_symbol} treasury return of "
                f"{self.label_of(operation.operator_address, readings=readings).lower()} on {chain.name} failed "
                f"({operation.failure_reason}); the next try waits "
                f"{int(TOP_UP_RETRY_AFTER_FAILURE.total_seconds() // 60)} minutes. Check the key's gas and the "
                "treasury Safe with `operator list-keys`, or move what it holds by hand.",
                columns={
                    "key": str(operation.operation_id),
                    "asset": operation.asset_symbol or "",
                    "holds": "",
                    "problem": f"treasury_return failed: {operation.failure_reason}",
                },
            )
            for operation in readings.recently_failed_treasury_returns
        )
        return findings

    def key_reference_of(self, address: str, *, readings: OperatorKeyReadings) -> str:
        state = next((state for state in readings.key_states if state.operator_address == address), None)
        return str(state.key_id) if state is not None else "new"

    # Writes

    async def apply(self, plan: OperatorKeyPlan) -> None:
        """Freezes, then each new operation in its own savepoint: the unique index on active top-ups turns a
        duplicate into a skipped insert, not a failed look."""
        chain = self.chain
        async with self.session_factory() as session, session.begin():
            key_service = OperatorKeyService.on_session(session)
            for freeze in plan.freezes:
                await key_service.freeze_key(
                    chain.key, operator_address=freeze.operator_address, role=freeze.role, reason=freeze.reason
                )
            for top_up in plan.top_ups:
                await self.insert_key_operation(
                    session,
                    operation=OutgoingOperation(
                        kind=OperationKind.TOP_UP,
                        status=OperationStatus.PENDING,
                        invoice_id=None,
                        chain_key=chain.key,
                        operator_address=top_up.operator_address,
                        # The Safe it pulls from; the runner refuses it once that is no longer the configured one.
                        destination_address=chain.treasury.treasury_safe_address,
                        recipient_address=top_up.recipient_address,
                        asset_symbol=top_up.asset.symbol,
                        token_address=top_up.asset.token_address,
                        amount_base_units=top_up.amount_base_units,
                    ),
                )
            for treasury_return in plan.treasury_returns:
                await self.insert_key_operation(
                    session,
                    operation=OutgoingOperation(
                        kind=OperationKind.TREASURY_RETURN,
                        status=OperationStatus.PENDING,
                        invoice_id=None,
                        chain_key=chain.key,
                        operator_address=treasury_return.operator_address,
                        recipient_address=chain.treasury.treasury_safe_address,
                        asset_symbol=treasury_return.asset.symbol,
                        token_address=treasury_return.asset.token_address,
                        amount_base_units=treasury_return.amount_base_units,
                    ),
                )

    async def insert_key_operation(self, session: AsyncSession, *, operation: OutgoingOperation) -> None:
        try:
            async with session.begin_nested():
                await SettlementRepository(session=session).insert_operation(operation)
        except IntegrityError:
            return
        log.emit(
            logger,
            event=KeyOperationRecorded(
                chain_key=operation.chain_key,
                operation_id=operation.id,
                kind=operation.kind.value,
                operator_address=operation.operator_address,
                recipient_address=operation.recipient_address,
                asset_symbol=operation.asset_symbol,
                amount_base_units=operation.amount_base_units,
            ),
        )

    # Report

    def report_of(self, readings: OperatorKeyReadings, *, plan: OperatorKeyPlan) -> OperatorKeyReport:
        chain = self.chain
        assets = top_up_assets_of(chain) if chain.treasury.is_enabled else [chain.native_asset]
        state_by_address = {state.operator_address: state for state in readings.key_states}
        addresses = list(dict.fromkeys([*self.key_addresses.all_addresses, *state_by_address]))

        def amount_text(amount: int | None, *, asset: AssetConfiguration) -> str:
            return "unreadable" if amount is None else format_base_units(amount, decimals=asset.decimals)

        rows: list[OperatorKeyRow] = []
        for address in addresses:
            state = state_by_address.get(address)
            role = self.key_addresses.role_of(address) or (state.role if state is not None else None)
            allowances = readings.allowances.get(address, {})
            rows.append(
                OperatorKeyRow(
                    key_id=state.key_id if state is not None else None,
                    operator_address=address,
                    role=role.value if role is not None else "",
                    status=state.status.value if state is not None else "not recorded yet",
                    reason=state.reason if state is not None else "",
                    configured=address in self.key_addresses.all_addresses,
                    latest_nonce=readings.latest_nonce_by_address.get(address),
                    allocator_next_nonce=readings.next_nonce_by_address.get(address),
                    active_operations=readings.active_operation_counts.get(address, 0),
                    balances={
                        asset.symbol: amount_text(readings.balances[address].get(asset.symbol), asset=asset)
                        for asset in assets
                        if address in readings.balances
                    },
                    allowances={
                        symbol: "unreadable"
                        if allowance is None
                        else f"{amount_text(allowance.available_at(block_timestamp=readings.block_timestamp), asset=asset)}"
                        f" of {amount_text(allowance.amount, asset=asset)} left"
                        for symbol, allowance in allowances.items()
                        if (asset := chain.find_asset(symbol)) is not None
                    },
                )
            )
        treasury_balances = {
            asset.symbol: amount_text(readings.treasury_balances.get(asset.symbol), asset=asset)
            for asset in assets
            if asset.symbol in readings.treasury_balances
        }
        treasury_text = (
            f"treasury Safe {chain.treasury.treasury_safe_address}"
            if chain.treasury.is_enabled
            else "no treasury Safe (top-ups off)"
        )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return OperatorKeyReport(
            summary=f"{len(rows)} keys ({', '.join(f'{count} {status}' for status, count in sorted(counts.items()))}); "
            f"{treasury_text}; {len(plan.top_ups)} top-ups and {len(plan.treasury_returns)} returns due",
            rows=rows,
            treasury_balances=treasury_balances,
            findings=plan.findings,
        )
