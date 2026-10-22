"""TreasuryService: the ``treasury`` CLI's side of the treasury Safe the operator keys are topped up from (2026-10-13;
PLAN.md section 5, "Treasury").

- ``create_safe``: records a ``treasury_safe_creation`` operation the watcher sends from the refund key, like any
  other (stored before broadcast, the nonce allocator, finality). Its SafeL2 proxy comes from SafeProxyFactory
  ``createProxyWithNonce`` with owners [owner], threshold 1 and the fallback handler, so the Safe's address follows
  from the chain, the owner and a ``saltNonce`` derived from both: running it again finds the same Safe.
- ``build_setup_calls``: what the Safe must do so the keys can pull top-ups: ``enableModule(AllowanceModule)``,
  ``addDelegate`` for every active configured key, ``setAllowance`` per key and per asset of its role's top-up rules
  (daily, ``resetTimeMin`` 1440), and ``removeDelegate`` (allowances included) for frozen or retired keys and for
  keys no longer configured (a key's state row outlives its environment variable). Each is
  left out when the chain already shows it, so a second run lists only what changed. As a Safe Transaction Builder
  batch file (``transaction_builder_batch_of``) the owner loads it in the Safe app and signs it there.
- ``execute_setup``: the same calls as one ``execTransaction`` delegatecall to MultiSendCallOnly, sent by the owner
  with its pre-validated signature (``v`` 1). It needs the owner's key in this process: only for fork e2e and test
  Safes, never a Safe holding real money.
- ``create_safe`` and ``execute_setup`` refuse an owner that is an operator key (configured or ever recorded): a
  server key must never own the treasury it is topped up from.
- ``read_status``: owners, threshold, the module, each key's delegate entry, and the key report the watcher works from.

Sources (checked 2026-10-13): Safe v1.4.1 ``Safe.sol`` (``setup``, ``execTransaction``, ``checkNSignatures``),
``SafeProxyFactory.sol``, ``MultiSendCallOnly.sol`` (https://github.com/safe-global/safe-smart-account/tree/v1.4.1),
AllowanceModule.sol at tag ``allowance/v0.1.1``
(https://github.com/safe-global/safe-modules/blob/allowance/v0.1.1/modules/allowances/contracts/AllowanceModule.sol),
the Transaction Builder's batch file and checksum (https://github.com/safe-global/safe-react-apps/tree/development/apps/tx-builder,
``src/lib/checksum.ts``, ``src/typings/models.ts``).
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json

from eth_account import Account
from eth_typing import HexStr
from eth_utils import keccak, to_checksum_address
from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import EvmRpcClient, EvmRpcEndpoint, decode_uint256
from app.infrastructure.evm_rpc_api.schemas import Multicall3Result, TransactionReceipt
from app.infrastructure.safe_api.client import (
    DAILY_RESET_MINUTES,
    OPERATION_DELEGATE_CALL,
    build_add_delegate_call,
    build_enable_module_call,
    build_remove_delegate_call,
    build_set_allowance_call,
    decode_bool,
    decode_delegate_slot,
    decode_owners,
    decode_token_allowance,
    encode_delegate_slot_call,
    encode_exec_transaction_by_sole_owner,
    encode_get_owners_call,
    encode_get_threshold_call,
    encode_get_token_allowance_call,
    encode_is_module_enabled_call,
    encode_multi_send,
    encode_safe_setup_initializer,
    predict_safe_proxy_address,
)
from app.infrastructure.safe_api.schemas import SafeCall
from app.payments.chains.types import EvmChainConfiguration, TopUpRule
from app.payments.settlements.enums import OperationKind, OperationStatus, OperatorKeyStatus
from app.payments.settlements.errors import TreasuryNotConfiguredError, TreasuryOwnerIsOperatorKeyError
from app.payments.settlements.models import OutgoingOperation
from app.payments.settlements.operation_transactions import GAS_LIMIT_MARGIN_FLAT
from app.payments.settlements.operator_key_maintenance_service import OperatorKeyMaintenanceService
from app.payments.settlements.operator_key_service import OperatorKeyService
from app.payments.settlements.operator_keys import OperatorKeyAddresses
from app.payments.settlements.repository import ACTIVE_STATUSES, SettlementRepository
from app.payments.settlements.types import OperatorKeyReport

type SessionFactory = Callable[[], AsyncSession]

SALT_NONCE_DOMAIN = b"pay-7331 treasury"
TRANSACTION_BUILDER_VERSION = "1.18.0"
SETUP_RECEIPT_WAIT_SECONDS = 120
SETUP_RECEIPT_POLL_SECONDS = 2


class TreasurySafeCreation(BaseModel):
    model_config = ConfigDict(frozen=True)

    safe_address: str
    # "deployed": code at the address already; "recorded": a new operation; "under_way": one already active.
    state: str
    operation_id: int | None


class TreasuryStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    safe_address: str
    owners: list[str] | None
    threshold: int | None
    module_enabled: bool | None
    # Key address to whether the Allowance Module lists it as the Safe's delegate (None: unreadable).
    delegates: dict[str, bool | None]
    pending_setup_calls: list[str]
    key_report: OperatorKeyReport


class SetupExecution(BaseModel):
    model_config = ConfigDict(frozen=True)

    transaction_hash: str | None
    succeeded: bool
    call_count: int


def treasury_salt_nonce_of(chain: EvmChainConfiguration, *, owner_address: str) -> int:
    """Deterministic per chain and owner, so ``create-safe`` finds its own Safe again."""
    return int.from_bytes(
        keccak(
            SALT_NONCE_DOMAIN
            + chain.chain_id.to_bytes(32, "big")
            + bytes.fromhex(to_checksum_address(owner_address).removeprefix("0x"))
        ),
        "big",
    )


def serialize_for_checksum(value: JsonValue) -> str:
    """The Transaction Builder's ``serializeJSONObject``: an object as its sorted key list then each value followed
    by a comma, an array element by element, anything else as ``JSON.stringify``."""
    if isinstance(value, list):
        return "[" + ",".join(serialize_for_checksum(element) for element in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value)
        return (
            "{"
            + json.dumps(keys, separators=(",", ":"), ensure_ascii=False)
            + "".join(serialize_for_checksum(value[key]) + "," for key in keys)
            + "}"
        )
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def transaction_builder_batch_of(
    calls: list[SafeCall], *, chain: EvmChainConfiguration, safe_address: str
) -> dict[str, JsonValue]:
    """A batch file the Safe app's Transaction Builder loads, its ``meta.checksum`` as ``calculateChecksum`` makes it
    (keccak of the batch serialized with ``meta.name`` null)."""
    meta: dict[str, JsonValue] = {
        "name": f"pay-7331 treasury setup ({chain.name})",
        "description": "; ".join(call.description for call in calls),
        "txBuilderVersion": TRANSACTION_BUILDER_VERSION,
        "createdFromSafeAddress": safe_address,
        "createdFromOwnerAddress": "",
    }
    batch: dict[str, JsonValue] = {
        "version": "1.0",
        "chainId": str(chain.chain_id),
        "createdAt": int(datetime.now(UTC).timestamp() * 1000),
        "meta": meta,
        "transactions": [{"to": call.to, "value": str(call.value), "data": "0x" + call.data.hex()} for call in calls],
    }
    checksum = "0x" + keccak(text=serialize_for_checksum({**batch, "meta": {**meta, "name": None}})).hex()
    return {**batch, "meta": {**meta, "checksum": checksum}}


class TreasuryService:
    def __init__(
        self,
        *,
        chain: EvmChainConfiguration,
        key_addresses: OperatorKeyAddresses,
        rpc_client: EvmRpcClient,
        session_factory: SessionFactory,
    ) -> None:
        self.chain = chain
        self.key_addresses = key_addresses
        self.rpc_client = rpc_client
        self.session_factory = session_factory

    def configured_safe_and_module(self) -> tuple[str, str]:
        treasury = self.chain.treasury
        if treasury.treasury_safe_address is None or treasury.allowance_module_address is None:
            raise TreasuryNotConfiguredError(
                f"no treasury Safe or Allowance Module for {self.chain.key} "
                f"(PAY_TREASURY_SAFE_ADDRESS_{self.chain.key.upper()}, chains.toml)"
            )
        return treasury.treasury_safe_address, treasury.allowance_module_address

    def top_up_rules_of_key(self, address: str) -> list[TopUpRule]:
        role = self.key_addresses.role_of(address)
        return self.chain.treasury.top_up_rules_of_role(role.value) if role is not None else []

    async def refuse_operator_key_owner(self, owner_address: str) -> None:
        async with self.session_factory() as session:
            key_states = await OperatorKeyService.on_session(session).list_key_states(self.chain.key)
        operator_addresses = {*self.key_addresses.all_addresses, *(state.operator_address for state in key_states)}
        if to_checksum_address(owner_address) in operator_addresses:
            raise TreasuryOwnerIsOperatorKeyError(
                "that address is an operator key: the treasury Safe's owner must be the owner's own wallet"
            )

    # create-safe

    async def create_safe(self, *, owner_address: str) -> TreasurySafeCreation:
        chain = self.chain
        owner_address = to_checksum_address(owner_address)
        await self.refuse_operator_key_owner(owner_address)
        salt_nonce = treasury_salt_nonce_of(chain, owner_address=owner_address)

        async def predict(endpoint: EvmRpcEndpoint) -> tuple[str, bool]:
            safe_address = await predict_safe_proxy_address(
                endpoint,
                proxy_factory_address=chain.treasury.safe_proxy_factory_address,
                singleton_address=chain.treasury.safe_singleton_address,
                initializer=encode_safe_setup_initializer(
                    owner_address=owner_address,
                    fallback_handler_address=chain.treasury.safe_fallback_handler_address,
                ),
                salt_nonce=salt_nonce,
            )
            return safe_address, bool(await endpoint.read_addresses_with_code([safe_address]))

        safe_address, deployed = await self.rpc_client.call_with_failover(predict)
        if deployed:
            return TreasurySafeCreation(safe_address=safe_address, state="deployed", operation_id=None)
        async with self.session_factory() as session, session.begin():
            repository = SettlementRepository(session=session)
            for operation in await repository.list_key_operations(chain.key, statuses=ACTIVE_STATUSES):
                if operation.kind == OperationKind.TREASURY_SAFE_CREATION and operation.recipient_address == (
                    safe_address
                ):
                    return TreasurySafeCreation(safe_address=safe_address, state="under_way", operation_id=operation.id)
            operation = await repository.insert_operation(
                OutgoingOperation(
                    kind=OperationKind.TREASURY_SAFE_CREATION,
                    status=OperationStatus.PENDING,
                    invoice_id=None,
                    chain_key=chain.key,
                    operator_address=self.key_addresses.refund_key_address,
                    destination_address=owner_address,
                    recipient_address=safe_address,
                    invoice_salt=salt_nonce.to_bytes(32, "big"),
                )
            )
            return TreasurySafeCreation(safe_address=safe_address, state="recorded", operation_id=operation.id)

    # setup-transactions, execute-setup

    async def build_setup_calls(self) -> list[SafeCall]:
        """What the Safe still has to do (module docstring), read at ``latest``."""
        chain = self.chain
        safe_address, module_address = self.configured_safe_and_module()
        async with self.session_factory() as session:
            key_states = await OperatorKeyService.on_session(session).list_key_states(chain.key)
        status_by_address = {state.operator_address: state.status for state in key_states}
        active_addresses = [
            address
            for address in self.key_addresses.all_addresses
            if status_by_address.get(address, OperatorKeyStatus.ACTIVE) == OperatorKeyStatus.ACTIVE
        ]
        removed_addresses = [
            state.operator_address
            for state in key_states
            if state.status != OperatorKeyStatus.ACTIVE
            or state.operator_address not in self.key_addresses.all_addresses
        ]
        wanted_allowances = [
            (address, asset, asset.base_units_of(rule.daily_allowance_amount))
            for address in active_addresses
            for rule in self.top_up_rules_of_key(address)
            if (asset := chain.find_asset(rule.asset_symbol)) is not None
        ]
        delegate_addresses = [*active_addresses, *removed_addresses]

        async def read(endpoint: EvmRpcEndpoint) -> list[Multicall3Result]:
            return await endpoint.aggregate3(
                [
                    encode_is_module_enabled_call(safe_address=safe_address, module_address=module_address),
                    *(
                        encode_delegate_slot_call(
                            allowance_module_address=module_address,
                            safe_address=safe_address,
                            delegate_address=address,
                        )
                        for address in delegate_addresses
                    ),
                    *(
                        encode_get_token_allowance_call(
                            allowance_module_address=module_address,
                            safe_address=safe_address,
                            delegate_address=address,
                            token_address=asset.token_address,
                        )
                        for address, asset, _ in wanted_allowances
                    ),
                ],
                block_hash=None,
            )

        results = await self.rpc_client.call_with_failover(read)
        module_enabled = decode_bool(results[0])
        delegate_slots = {
            address: decode_delegate_slot(result)
            for address, result in zip(delegate_addresses, results[1 : 1 + len(delegate_addresses)], strict=True)
        }
        allowance_results = results[1 + len(delegate_addresses) :]
        calls: list[SafeCall] = []
        if module_enabled is not True:
            calls.append(build_enable_module_call(safe_address=safe_address, module_address=module_address))
        calls.extend(
            build_add_delegate_call(allowance_module_address=module_address, delegate_address=address)
            for address in active_addresses
            if delegate_slots.get(address) != address
        )
        for (address, asset, allowance_base_units), result in zip(wanted_allowances, allowance_results, strict=True):
            allowance = decode_token_allowance(result)
            if (
                allowance is None
                or allowance.amount != allowance_base_units
                or allowance.reset_time_minutes != DAILY_RESET_MINUTES
            ):
                calls.append(
                    build_set_allowance_call(
                        allowance_module_address=module_address,
                        delegate_address=address,
                        token_address=asset.token_address,
                        allowance_base_units=allowance_base_units,
                    )
                )
        calls.extend(
            build_remove_delegate_call(allowance_module_address=module_address, delegate_address=address)
            for address in removed_addresses
            if delegate_slots.get(address) == address
        )
        return calls

    async def execute_setup(self, *, owner_private_key: SecretStr) -> SetupExecution:
        """The setup calls in one ``execTransaction`` sent by the owner (module docstring: test Safes only)."""
        chain = self.chain
        safe_address, _ = self.configured_safe_and_module()
        owner_account = Account.from_key(owner_private_key.get_secret_value())
        await self.refuse_operator_key_owner(owner_account.address)
        calls = await self.build_setup_calls()
        if not calls:
            return SetupExecution(transaction_hash=None, succeeded=True, call_count=0)
        call_data = encode_exec_transaction_by_sole_owner(
            to=chain.treasury.safe_multi_send_call_only_address,
            value=0,
            data=encode_multi_send(calls),
            operation=OPERATION_DELEGATE_CALL,
            owner_address=owner_account.address,
        )

        async def send(endpoint: EvmRpcEndpoint) -> str:
            block_heads = await endpoint.read_block_heads()
            priority_fee = max(await endpoint.read_max_priority_fee_per_gas(), chain.minimum_priority_fee_per_gas_wei)
            fee_cap = chain.maximum_fee_per_gas_wei
            estimated_gas = await endpoint.estimate_gas(
                sender_address=owner_account.address, target_address=safe_address, call_data=call_data, value=0
            )
            signed = owner_account.sign_transaction(
                {
                    "type": 2,
                    "chainId": chain.chain_id,
                    "nonce": await endpoint.read_transaction_count(owner_account.address, block_tag="pending"),
                    "to": HexStr(safe_address),
                    "value": 0,
                    "data": call_data,
                    "gas": estimated_gas * 3 // 2 + GAS_LIMIT_MARGIN_FLAT,
                    "maxFeePerGas": min(2 * block_heads.latest.base_fee_per_gas + priority_fee, fee_cap),
                    "maxPriorityFeePerGas": min(priority_fee, fee_cap),
                }
            )
            await endpoint.send_raw_transaction(bytes(signed.raw_transaction))
            return "0x" + bytes(signed.hash).hex()

        transaction_hash = await self.rpc_client.call_with_failover(send)
        receipt = await self.wait_for_receipt(transaction_hash)
        return SetupExecution(
            transaction_hash=transaction_hash,
            succeeded=receipt is not None and receipt.succeeded,
            call_count=len(calls),
        )

    async def wait_for_receipt(self, transaction_hash: str) -> TransactionReceipt | None:
        for _ in range(SETUP_RECEIPT_WAIT_SECONDS // SETUP_RECEIPT_POLL_SECONDS):

            async def read(endpoint: EvmRpcEndpoint) -> TransactionReceipt | None:
                (receipt,) = await endpoint.read_transaction_receipts([transaction_hash])
                return receipt

            receipt = await self.rpc_client.call_with_failover(read)
            if receipt is not None:
                return receipt
            await asyncio.sleep(SETUP_RECEIPT_POLL_SECONDS)
        return None

    # status

    async def read_status(self) -> TreasuryStatus:
        safe_address, module_address = self.configured_safe_and_module()
        addresses = self.key_addresses.all_addresses

        async def read(endpoint: EvmRpcEndpoint) -> tuple[list[Multicall3Result], OperatorKeyReport]:
            results = await endpoint.aggregate3(
                [
                    encode_get_owners_call(safe_address),
                    encode_get_threshold_call(safe_address),
                    encode_is_module_enabled_call(safe_address=safe_address, module_address=module_address),
                    *(
                        encode_delegate_slot_call(
                            allowance_module_address=module_address,
                            safe_address=safe_address,
                            delegate_address=address,
                        )
                        for address in addresses
                    ),
                ],
                block_hash=None,
            )
            key_report = await OperatorKeyMaintenanceService(
                chain=self.chain,
                key_addresses=self.key_addresses,
                session_factory=self.session_factory,
                rpc_client=self.rpc_client,
            ).build_report(endpoint, block_heads=await endpoint.read_block_heads())
            return results, key_report

        results, key_report = await self.rpc_client.call_with_failover(read)
        threshold_result = results[1]
        return TreasuryStatus(
            safe_address=safe_address,
            owners=decode_owners(results[0]),
            threshold=decode_uint256(threshold_result.return_data) if threshold_result.succeeded else None,
            module_enabled=decode_bool(results[2]),
            delegates={
                address: None if (slot := decode_delegate_slot(result)) is None else slot == address
                for address, result in zip(addresses, results[3:], strict=True)
            },
            pending_setup_calls=[call.description for call in await self.build_setup_calls()],
            key_report=key_report,
        )
