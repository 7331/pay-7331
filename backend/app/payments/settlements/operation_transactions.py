"""What each outgoing operation's transaction is, and its fees and gas limit (PLAN.md section 5, step 6).

Split from ``runner_service.py`` (2026-10-13), which decides when to sign, send, bump and resolve. Per kind:

- sweep: the factory's ``sweepToDestination`` for the deposit, listing only the tokens it holds at ``latest`` and
  none the owner skipped (``sweep-invoice --skip-token``). The contract always forwards the native coin.
- refund: the refund key's own native coin or token ``transfer`` to the recorded sender.
- top_up: ``executeAllowanceTransfer`` of the amount from the treasury Safe to the key topped up (its
  ``recipient_address``, the module's delegate), never with a relayer payment. Sent by that key itself, with no
  signature; or, when it holds too little gas for its own top-up (2026-10-13), by another operator key (the
  operation's ``operator_address``) carrying the delegate's signature over the module's transfer hash at its
  allowance nonce read at ``latest`` when its first attempt is signed. Its replacements resend that first call
  (``stored_call_of``), never a new signature: the signature does not bind who sends it, so one at a newer nonce
  would let the first attempt's call, executed by anyone, and the replacement both pay the key. The Safe is the one
  the top-up was made for and must still be the configured one, and the delegate a configured operator key.
- treasury_return: a retired key's native coin or token ``transfer`` to the treasury Safe. Its recipient must be the
  configured treasury Safe when it is signed, or it is refused: the only path money leaves a key for anywhere but a
  sweep, refund or top-up target. A native return's amount is fixed when it is made, as the key's balance less a gas
  reserve sized from the node's estimate at that moment (``treasury_return_gas_reserve_of``), and never changes: the
  first attempt is signed only if the balance pays amount and its gas limit at the fee cap, and a replacement (same
  nonce, same call, a fresh estimate) only if it still does, else the newest attempt is rebroadcast
  (``attempt_replacement.py``). So whichever attempt mines moves exactly the recorded amount to the Safe.
- treasury_safe_creation: the refund key calls SafeProxyFactory ``createProxyWithNonce`` with a SafeL2 ``setup``
  of one owner, threshold 1 and the fallback handler (``treasury create-safe``).

Fees are EIP-1559: tip = max(node suggestion, chain minimum), max fee = 2 x base fee + tip, both capped at the chain's
``maximum_fee_per_gas_gwei``.
"""

import math

from eth_abi import encode
from eth_account.signers.local import LocalAccount
from eth_account.typed_transactions import TypedTransaction
from eth_typing import Hash32, HexStr
from eth_utils import function_signature_to_4byte_selector, to_checksum_address
from hexbytes import HexBytes
from pydantic import BaseModel, ConfigDict

from app.infrastructure.deposit_factory_api.client import DepositFactoryApiClient
from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint, decode_uint256, encode_token_balance_call
from app.infrastructure.evm_rpc_api.errors import EvmExecutionRevertedError, EvmInsufficientFundsError
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, TransactionReceipt
from app.infrastructure.safe_api.client import (
    allowance_transfer_hash_of,
    decode_token_allowance,
    encode_create_proxy_with_nonce,
    encode_execute_allowance_transfer_to_delegate,
    encode_get_token_allowance_call,
    encode_safe_setup_initializer,
)
from app.payments.chains.types import EvmChainConfiguration
from app.payments.settlements.enums import OperationKind, OperationStatus, OperatorKeyStatus
from app.payments.settlements.errors import PassReadUnavailableError, TreasuryNotConfiguredError
from app.payments.settlements.models import OutgoingAttempt, OutgoingOperation
from app.payments.settlements.operator_keys import OperatorKeyRing
from app.payments.settlements.types import (
    OPERATOR_KEY_RETIRED_FAILURE_REASON,
    OPERATOR_KEY_UNUSABLE_FAILURE_REASON,
    TOP_UP_RECIPIENT_UNUSABLE_FAILURE_REASON,
)

# Replacement caps: geth and bor need at least 10% more on both; 12.5% clears PLAN.md's 12%.
FEE_BUMP_NUMERATOR = 9
FEE_BUMP_DENOMINATOR = 8
GAS_LIMIT_MARGIN_NUMERATOR = 5
GAS_LIMIT_MARGIN_DENOMINATOR = 4
GAS_LIMIT_MARGIN_FLAT = 10_000
NATIVE_TRANSFER_GAS = 21_000
# A sweep lists the tokens the deposit holds when its attempt is signed; native dust landing between the estimate
# and the send (anyone can front-run), or a replacement that lists a token received since, adds a transfer the
# estimate did not see. About 30k each measured on a fork (2026-10-09). Headroom for every configured asset, so any
# replacement fits the first attempt's limit; unused gas is not charged.
SWEEP_GAS_HEADROOM_PER_ASSET = 40_000
# A native return to the Safe runs the proxy's receive (an event, about 30k gas in all). Its amount leaves behind
# this many times the gas limit its estimate asks for, at the fee cap (2026-10-14), so a rollup's parent-chain posting
# fee can rise between the look, the first signing and every replacement without a replacement outgrowing the balance.
TREASURY_RETURN_GAS_RESERVE_HEADROOM = 2
# The reserve's gas when the node will not estimate the return (the first attempt then fails at its own estimate).
TREASURY_RETURN_FALLBACK_GAS = 60_000
TRANSFER_SELECTOR = function_signature_to_4byte_selector("transfer(address,uint256)")


class SettlementPassReadings(BaseModel):
    """The reads a pass's steps share (runner_service.py's docstring), read once before the first step."""

    model_config = ConfigDict(frozen=True)

    block_heads: BlockHeads
    # Every attempt of a ``broadcast`` operation and the transaction of a ``mined`` one whose block is final; None is
    # a transaction this endpoint has not mined (or does not know), as one receipt read says. A hash whose read failed
    # is absent: ``receipt_of`` raises for it, so only its operation's step fails.
    receipts_by_transaction_hash: dict[str, TransactionReceipt | None]
    # Read only for the keys of ``broadcast`` operations with no receipt, one batch; a key whose read failed is absent.
    latest_nonce_by_address: dict[str, int]
    # Read only when a step may sign: a ``pending`` or ``waiting_for_float`` operation, or a possible fee bump. The
    # chain's minimum tip when the node's suggestion could not be read.
    suggested_max_priority_fee_per_gas: int | None
    # The chain's key states (``operator_key_states``); a key with no row yet is active.
    key_status_by_address: dict[str, OperatorKeyStatus]

    def key_status_of(self, address: str) -> OperatorKeyStatus:
        return self.key_status_by_address.get(address, OperatorKeyStatus.ACTIVE)

    def is_recorded_active_key(self, address: str) -> bool:
        """``active`` with a key state row: a key the watcher has not adopted yet (its first minute's look records
        it, ``operator_key_service.py``) signs nothing, so its nonce row is never taken before it is adopted."""
        return self.key_status_by_address.get(address) == OperatorKeyStatus.ACTIVE

    def receipt_of(self, transaction_hash: str) -> TransactionReceipt | None:
        if transaction_hash not in self.receipts_by_transaction_hash:
            raise PassReadUnavailableError(f"the pass could not read the receipt of {transaction_hash}")
        return self.receipts_by_transaction_hash[transaction_hash]


def raised_fee(previous: int, *, current: int) -> int:
    return max(math.ceil(previous * FEE_BUMP_NUMERATOR / FEE_BUMP_DENOMINATOR), current)


def fee_caps_of(chain: EvmChainConfiguration, *, readings: SettlementPassReadings) -> tuple[int, int]:
    """(max fee, tip) per PLAN.md section 5, neither above the chain's fee cap."""
    if readings.suggested_max_priority_fee_per_gas is None:
        raise ValueError("the pass read no priority fee suggestion")
    fee_cap = chain.maximum_fee_per_gas_wei
    priority_fee = min(
        max(readings.suggested_max_priority_fee_per_gas, chain.minimum_priority_fee_per_gas_wei), fee_cap
    )
    return min(2 * readings.block_heads.latest.base_fee_per_gas + priority_fee, fee_cap), priority_fee


def replacement_fee_caps_of(
    chain: EvmChainConfiguration, *, newest_attempt: OutgoingAttempt, readings: SettlementPassReadings
) -> tuple[int, int] | None:
    """(max fee, tip) of a replacement: both raised by the replacement margin over the newest attempt, at least the
    current ones, under the chain's fee cap; None when a full raise no longer fits (the attempt waits for the base
    fee to come down)."""
    fee_cap = chain.maximum_fee_per_gas_wei
    if raised_fee(newest_attempt.max_fee_per_gas, current=0) > fee_cap:
        return None
    current_max_fee, current_priority_fee = fee_caps_of(chain, readings=readings)
    priority_fee = min(raised_fee(newest_attempt.max_priority_fee_per_gas, current=current_priority_fee), fee_cap)
    max_fee = min(
        max(
            raised_fee(newest_attempt.max_fee_per_gas, current=current_max_fee),
            2 * readings.block_heads.latest.base_fee_per_gas + priority_fee,
        ),
        fee_cap,
    )
    return max_fee, priority_fee


def stored_call_of(attempt: OutgoingAttempt) -> tuple[str, bytes, int]:
    """The target, call data and value a stored attempt was signed with (the EIP-1559 transaction
    ``sign_operation_attempt`` signs)."""
    transaction = TypedTransaction.from_bytes(HexBytes(attempt.raw_transaction)).as_dict()
    return to_checksum_address(transaction["to"]), bytes(transaction["data"]), int(transaction["value"])


def waits_for_finality(operation: OutgoingOperation, *, finalized_block_number: int) -> bool:
    """A ``mined`` operation whose block is above the finalized head (or that lacks its block or transaction) has no
    step this pass: it needs no read at all."""
    return operation.status == OperationStatus.MINED and (
        operation.block_number is None
        or operation.transaction_hash is None
        or finalized_block_number < operation.block_number
    )


def native_transfer_gas_limit_of(estimated_gas: int) -> int:
    """21,000 for a native transfer to a plain account; else the estimate with a margin. On a rollup the node's
    estimate of a plain transfer is above 21,000, since it includes the L1 data fee as gas (24,296 on Arbitrum One,
    2026-10-14), and a limit of 21,000 would not pay it; the estimate is always asked for, never assumed."""
    if estimated_gas <= NATIVE_TRANSFER_GAS:
        return NATIVE_TRANSFER_GAS
    return estimated_gas * GAS_LIMIT_MARGIN_NUMERATOR // GAS_LIMIT_MARGIN_DENOMINATOR + GAS_LIMIT_MARGIN_FLAT


def treasury_return_gas_reserve_of(chain: EvmChainConfiguration, *, estimated_gas: int | None) -> int:
    """Wei a native treasury return leaves on its key: the headroom times the gas limit its estimate asks for, at the
    chain's fee cap (``TREASURY_RETURN_GAS_RESERVE_HEADROOM``); the fixed fallback gas when there is no estimate."""
    gas = (
        TREASURY_RETURN_FALLBACK_GAS
        if estimated_gas is None
        else TREASURY_RETURN_GAS_RESERVE_HEADROOM * native_transfer_gas_limit_of(estimated_gas)
    )
    return gas * chain.maximum_fee_per_gas_wei


async def read_native_return_gas_estimates(
    endpoint: EvmRpcEndpoint, *, chain: EvmChainConfiguration, key_addresses: list[str]
) -> dict[str, int | None]:
    """``eth_estimateGas`` of a native transfer from each key to the treasury Safe, as a retired key's return would
    send it. The amount is not known yet (it is what is left after the reserve this estimate sizes), so the estimate
    is of a transfer of nothing, which no balance can refuse; the first attempt estimates the real amount again and
    checks the balance against it (runner_service.py). None when the node says it reverts or the key cannot pay."""
    if chain.treasury.treasury_safe_address is None:
        return {}
    estimates: dict[str, int | None] = {}
    for key_address in key_addresses:
        try:
            estimates[key_address] = await endpoint.estimate_gas(
                sender_address=key_address, target_address=chain.treasury.treasury_safe_address, call_data=b"", value=0
            )
        except (EvmExecutionRevertedError, EvmInsufficientFundsError):
            estimates[key_address] = None
    return estimates


async def key_covers_value_and_gas(
    endpoint: EvmRpcEndpoint, *, operator_address: str, value: int, gas_cost: int
) -> bool:
    """The key's native balance at ``latest`` pays ``value`` and ``gas_cost``, as a node checks a transaction."""
    return await endpoint.read_native_balance(operator_address) >= value + gas_cost


def gas_limit_of(
    operation: OutgoingOperation, *, chain: EvmChainConfiguration, call_data: bytes, estimated_gas: int
) -> int:
    """``native_transfer_gas_limit_of`` for a native transfer. A native transfer can cost more than 21,000 (an EIP-7702
    account or a contract runs code on receipt; 21,000 reverted there, 2026-10-09), and a sweep gets headroom for every
    asset it may move."""
    if call_data == b"":
        return native_transfer_gas_limit_of(estimated_gas)
    gas_limit = estimated_gas * GAS_LIMIT_MARGIN_NUMERATOR // GAS_LIMIT_MARGIN_DENOMINATOR + GAS_LIMIT_MARGIN_FLAT
    if operation.kind.is_deposit_sweep:
        gas_limit += SWEEP_GAS_HEADROOM_PER_ASSET * (len(chain.token_addresses) + 1)
    return gas_limit


def salt_nonce_of(operation: OutgoingOperation) -> int:
    """A Safe creation keeps its ``saltNonce`` in the 32-byte ``invoice_salt`` column."""
    if operation.invoice_salt is None:
        raise ValueError(f"treasury Safe creation {operation.id} lacks its salt nonce")
    return int.from_bytes(operation.invoice_salt, "big")


def build_treasury_safe_creation_call_data(
    chain: EvmChainConfiguration, *, owner_address: str, salt_nonce: int
) -> bytes:
    return encode_create_proxy_with_nonce(
        singleton_address=chain.treasury.safe_singleton_address,
        initializer=encode_safe_setup_initializer(
            owner_address=owner_address, fallback_handler_address=chain.treasury.safe_fallback_handler_address
        ),
        salt_nonce=salt_nonce,
    )


def build_transfer(operation: OutgoingOperation) -> tuple[str, bytes, int]:
    """A native value transfer or a token ``transfer`` of the operation's amount to its recipient."""
    if operation.recipient_address is None or operation.amount_base_units is None:
        raise ValueError(f"{operation.kind.value} {operation.id} lacks its recipient or amount")
    if operation.token_address is None:
        return operation.recipient_address, b"", operation.amount_base_units
    call_data = TRANSFER_SELECTOR + encode(
        ["address", "uint256"], [operation.recipient_address, operation.amount_base_units]
    )
    return operation.token_address, call_data, 0


def key_operation_refusal_of(
    operation: OutgoingOperation, *, ring: OperatorKeyRing, readings: SettlementPassReadings
) -> str | None:
    """Why the key of a top-up or treasury return cannot sign it (it then fails before any nonce is spent); None
    when it can."""
    status = readings.key_status_of(operation.operator_address)
    if ring.find_account(operation.operator_address) is None or status == OperatorKeyStatus.FROZEN:
        return OPERATOR_KEY_UNUSABLE_FAILURE_REASON
    if operation.kind != OperationKind.TOP_UP:
        return None
    if status != OperatorKeyStatus.ACTIVE:
        # A retired key takes in nothing more: what it holds is going back to the treasury.
        return OPERATOR_KEY_RETIRED_FAILURE_REASON
    recipient_address = operation.recipient_address or ""
    if recipient_address != operation.operator_address and (
        ring.find_account(recipient_address) is None
        or readings.key_status_of(recipient_address) != OperatorKeyStatus.ACTIVE
    ):
        # Sent for another key (operator_key_maintenance_service.py): that key must be active too, so no allowance
        # flows into a frozen or retired one.
        return TOP_UP_RECIPIENT_UNUSABLE_FAILURE_REASON
    return None


def sign_operation_attempt(
    operation: OutgoingOperation,
    *,
    account: LocalAccount,
    chain: EvmChainConfiguration,
    nonce: int,
    target_address: str,
    call_data: bytes,
    value: int,
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> OutgoingAttempt:
    """An EIP-1559 transaction signed by the operation's key, as the attempt row stored before it is sent."""
    signed = account.sign_transaction(
        {
            "type": 2,
            "chainId": chain.chain_id,
            "nonce": nonce,
            "to": HexStr(target_address),
            "value": value,
            "data": call_data,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
        }
    )
    return OutgoingAttempt(
        operation_id=operation.id,
        raw_transaction=bytes(signed.raw_transaction),
        transaction_hash="0x" + bytes(signed.hash).hex(),
        gas_limit=gas_limit,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )


class OperationTransactionBuilder:
    def __init__(
        self,
        *,
        factory_clients: dict[str, DepositFactoryApiClient],
        operator_key_rings: dict[str, OperatorKeyRing],
    ) -> None:
        self.factory_clients = factory_clients
        # A top-up sent for another key signs with that key's account (module docstring).
        self.operator_key_rings = operator_key_rings

    async def read_held_token_addresses(
        self, operation: OutgoingOperation, *, chain: EvmChainConfiguration, endpoint: EvmRpcEndpoint
    ) -> list[str]:
        """The configured tokens the sweep's deposit holds at ``latest`` (a token whose balance read fails counts as
        held). A sweep lists only these: the contract calls every listed token and one failing call reverts the whole
        sweep, so an unrelated token can never block a deposit, and an empty token costs no gas
        (2026-10-11, when the list grew to 9 tokens). A token that arrives later is a stray the rescan sweeps. The
        operation's ``skipped_token_addresses`` are never listed: the owner leaves out a token whose transfer reverts
        (USDC's ``notBlacklisted`` on the deposit or destination, a paused token) so the rest of the deposit moves
        (``sweep-invoice --skip-token``, 2026-10-13)."""
        listable_token_addresses = [
            token_address
            for token_address in chain.token_addresses
            if token_address not in operation.skipped_token_addresses
        ]
        if operation.deposit_address is None or not listable_token_addresses:
            return listable_token_addresses
        results = await endpoint.aggregate3(
            [
                encode_token_balance_call(token_address, holder_address=operation.deposit_address)
                for token_address in listable_token_addresses
            ],
            block_hash=None,
        )
        return [
            token_address
            for token_address, result in zip(listable_token_addresses, results, strict=True)
            if not result.succeeded or decode_uint256(result.return_data) > 0
        ]

    async def transaction_call_of(
        self, operation: OutgoingOperation, *, chain: EvmChainConfiguration, endpoint: EvmRpcEndpoint
    ) -> tuple[str, bytes, int]:
        """Target, calldata and value of the operation's transaction. A key operation whose treasury is no longer
        the configured one raises ``TreasuryNotConfiguredError``."""
        match operation.kind:
            case OperationKind.SWEEP | OperationKind.STATIC_DEPOSIT_SWEEP:
                if operation.invoice_salt is None or operation.destination_address is None:
                    raise ValueError(f"sweep {operation.id} lacks its salt or destination")
                call_data = self.factory_clients[chain.key].encode_sweep_call(
                    invoice_salt=operation.invoice_salt,
                    destination_address=operation.destination_address,
                    token_addresses=await self.read_held_token_addresses(operation, chain=chain, endpoint=endpoint),
                )
                return self.factory_clients[chain.key].factory_address, call_data, 0
            case OperationKind.REFUND:
                return build_transfer(operation)
            case OperationKind.TOP_UP:
                return await self.top_up_call_of(operation, chain=chain, endpoint=endpoint)
            case OperationKind.TREASURY_RETURN:
                if (
                    chain.treasury.treasury_safe_address is None
                    or operation.recipient_address != chain.treasury.treasury_safe_address
                ):
                    raise TreasuryNotConfiguredError(
                        f"treasury return {operation.id}: its recipient is not the configured treasury Safe"
                    )
                return build_transfer(operation)
            case OperationKind.TREASURY_SAFE_CREATION:
                if operation.destination_address is None:
                    raise ValueError(f"treasury Safe creation {operation.id} lacks its owner")
                call_data = build_treasury_safe_creation_call_data(
                    chain, owner_address=operation.destination_address, salt_nonce=salt_nonce_of(operation)
                )
                return chain.treasury.safe_proxy_factory_address, call_data, 0

    async def top_up_call_of(
        self, operation: OutgoingOperation, *, chain: EvmChainConfiguration, endpoint: EvmRpcEndpoint
    ) -> tuple[str, bytes, int]:
        """The top-up's ``executeAllowanceTransfer`` (module docstring): no signature when the key tops itself up,
        else its delegate signature at the allowance nonce read now."""
        treasury = chain.treasury
        delegate_account = (
            self.operator_key_rings[chain.key].find_account(operation.recipient_address)
            if operation.recipient_address is not None
            else None
        )
        if (
            treasury.allowance_module_address is None
            or treasury.treasury_safe_address is None
            or operation.destination_address != treasury.treasury_safe_address
            or delegate_account is None
            or operation.amount_base_units is None
        ):
            raise TreasuryNotConfiguredError(
                f"top-up {operation.id}: its treasury Safe or the key it tops up is not configured"
            )
        delegate_signature = b""
        if delegate_account.address != operation.operator_address:
            (allowance_result,) = await endpoint.aggregate3(
                [
                    encode_get_token_allowance_call(
                        allowance_module_address=treasury.allowance_module_address,
                        safe_address=treasury.treasury_safe_address,
                        delegate_address=delegate_account.address,
                        token_address=operation.token_address,
                    )
                ],
                block_hash=None,
            )
            allowance = decode_token_allowance(allowance_result)
            if allowance is None:
                raise PassReadUnavailableError(f"top-up {operation.id}: the delegate's allowance nonce is unreadable")
            transfer_hash = allowance_transfer_hash_of(
                chain_id=chain.chain_id,
                allowance_module_address=treasury.allowance_module_address,
                safe_address=treasury.treasury_safe_address,
                token_address=operation.token_address,
                to_address=delegate_account.address,
                amount_base_units=operation.amount_base_units,
                allowance_nonce=allowance.nonce,
            )
            delegate_signature = bytes(delegate_account.unsafe_sign_hash(Hash32(transfer_hash)).signature)
        call_data = encode_execute_allowance_transfer_to_delegate(
            safe_address=treasury.treasury_safe_address,
            token_address=operation.token_address,
            delegate_address=delegate_account.address,
            amount_base_units=operation.amount_base_units,
            delegate_signature=delegate_signature,
        )
        return treasury.allowance_module_address, call_data, 0
