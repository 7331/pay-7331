"""The reads a settlement pass's steps share (``runner_service.py``'s docstring), split from it (2026-10-13).

Each is one request or one JSON-RPC batch: the receipts of every ``broadcast`` attempt and of every ``mined``
operation whose block is final, then the nonce at ``latest`` of each key with a ``broadcast`` operation lacking a
receipt, then the node's priority fee suggestion. A batch that fails is read again one request at a time, so one
transaction or key an endpoint refuses fails only the steps that need it (``SettlementPassReadings.receipt_of``, a
missing nonce), never the whole pass (PLAN.md section 5, step 6: one operation failing never stalls the others). A
failing tip suggestion falls back to the chain's minimum tip, which every fee already starts from.
"""

from collections.abc import Awaitable, Callable

from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.errors import EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, TransactionReceipt
from app.payments.chains.types import EvmChainConfiguration
from app.payments.settlements.enums import OperationStatus, OperatorKeyStatus
from app.payments.settlements.log_events import PassBatchReadFailed, PassSingleReadFailed
from app.payments.settlements.models import OutgoingAttempt, OutgoingOperation
from app.payments.settlements.operation_transactions import SettlementPassReadings
from app.shared import logging as log

logger = log.get_logger(__name__)

SIGNING_STATUSES = {OperationStatus.PENDING, OperationStatus.WAITING_FOR_FLOAT}


async def read_batch_or_each[Value](
    keys: list[str],
    *,
    read_name: str,
    read_batch: Callable[[list[str]], Awaitable[list[Value]]],
) -> dict[str, Value]:
    """``read_batch`` of every key; when the batch fails, each key alone, leaving out those that fail."""
    if not keys:
        return {}
    try:
        return dict(zip(keys, await read_batch(keys), strict=True))
    except EvmRpcUnavailableError:
        log.emit(logger, event=PassBatchReadFailed(read_name=read_name))
    values: dict[str, Value] = {}
    for key in keys:
        try:
            (values[key],) = await read_batch([key])
        except EvmRpcUnavailableError:
            log.emit(logger, event=PassSingleReadFailed(read_name=read_name))
    return values


async def read_settlement_pass_readings(
    operations: list[OutgoingOperation],
    *,
    chain: EvmChainConfiguration,
    endpoint: EvmRpcEndpoint,
    block_heads: BlockHeads,
    attempts_by_operation_id: dict[int, list[OutgoingAttempt]],
    key_status_by_address: dict[str, OperatorKeyStatus],
) -> SettlementPassReadings:
    """In the order a ``broadcast`` step read them before: receipts, then the keys' nonces at ``latest``."""
    transaction_hashes: list[str] = []
    for operation in operations:
        if operation.status == OperationStatus.BROADCAST:
            transaction_hashes.extend(
                attempt.transaction_hash for attempt in attempts_by_operation_id.get(operation.id, [])
            )
        elif operation.status == OperationStatus.MINED and operation.transaction_hash is not None:
            transaction_hashes.append(operation.transaction_hash)
    receipts_by_transaction_hash: dict[str, TransactionReceipt | None] = await read_batch_or_each(
        list(dict.fromkeys(transaction_hashes)), read_name="receipts", read_batch=endpoint.read_transaction_receipts
    )
    # A key is read when every attempt of one of its broadcast operations was read and none has a receipt; one whose
    # receipt read failed has nothing to decide this pass.
    addresses_without_receipt = list(
        dict.fromkeys(
            operation.operator_address
            for operation in operations
            if operation.status == OperationStatus.BROADCAST
            and all(
                attempt.transaction_hash in receipts_by_transaction_hash
                and receipts_by_transaction_hash[attempt.transaction_hash] is None
                for attempt in attempts_by_operation_id.get(operation.id, [])
            )
        )
    )

    async def read_latest_nonces(addresses: list[str]) -> list[int]:
        return await endpoint.read_transaction_counts(addresses, block_tag="latest")

    latest_nonce_by_address = await read_batch_or_each(
        addresses_without_receipt, read_name="latest_nonces", read_batch=read_latest_nonces
    )
    suggested_max_priority_fee_per_gas = None
    if addresses_without_receipt or any(operation.status in SIGNING_STATUSES for operation in operations):
        try:
            suggested_max_priority_fee_per_gas = await endpoint.read_max_priority_fee_per_gas()
        except EvmRpcUnavailableError:
            log.emit(logger, event=PassBatchReadFailed(read_name="priority_fee"))
            suggested_max_priority_fee_per_gas = chain.minimum_priority_fee_per_gas_wei
    return SettlementPassReadings(
        block_heads=block_heads,
        receipts_by_transaction_hash=receipts_by_transaction_hash,
        latest_nonce_by_address=latest_nonce_by_address,
        suggested_max_priority_fee_per_gas=suggested_max_priority_fee_per_gas,
        key_status_by_address=key_status_by_address,
    )
