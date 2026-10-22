"""Typed log records of outgoing transactions. Never a key or a raw transaction; hashes and ids are public."""

import logging

from app.shared.logging import LogEvent


class OperationBroadcast(LogEvent):
    message = "Outgoing transaction signed, stored and broadcast"
    operation = "settlement_broadcast"
    operation_id: int
    kind: str
    nonce: int
    transaction_hash: str
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


class OperationFeeBumped(LogEvent):
    message = "Outgoing transaction replaced at the same nonce with higher fees"
    operation = "settlement_fee_bump"
    operation_id: int
    nonce: int
    transaction_hash: str
    max_fee_per_gas: int


class OperationMined(LogEvent):
    message = "Outgoing transaction mined"
    operation = "settlement_mined"
    operation_id: int
    transaction_hash: str
    block_number: int
    succeeded: bool


class OperationResolved(LogEvent):
    message = "Outgoing operation resolved"
    operation = "settlement_resolved"
    operation_id: int
    kind: str
    status: str
    failure_reason: str | None


class OperationWaitingForFloat(LogEvent):
    level = logging.WARNING
    message = "A refund waits: the operator float cannot cover it"
    operation = "settlement_float"
    operation_id: int
    asset_symbol: str


class OperationStepFailed(LogEvent):
    level = logging.ERROR
    message = "One step of an outgoing operation failed; the others went on, it is retried next pass"
    operation = "settlement_step"
    operation_id: int


class ReceiptEndpointSkipped(LogEvent):
    level = logging.WARNING
    message = "An endpoint did not answer a receipt check before a nonce-taken decision; the others decide"
    operation = "settlement_step"
    operation_id: int


class OperatorGasShort(LogEvent):
    level = logging.WARNING
    message = "The operator key cannot pay an operation's gas; it waits for gas"
    operation = "settlement_gas"
    operation_id: int
    chain_key: str
    operator_address: str


class OperationWaitingForUsableKey(LogEvent):
    level = logging.WARNING
    message = "No usable operator key may sign this operation (frozen or retired); it waits"
    operation = "settlement_key"
    operation_id: int
    kind: str
    operator_address: str


class OperationReassignedToKey(LogEvent):
    message = "A pending sweep moved to another sweeper key (its key is frozen, retired or no longer configured)"
    operation = "settlement_key"
    operation_id: int
    previous_operator_address: str
    operator_address: str


class OperatorKeyStatusChanged(LogEvent):
    level = logging.WARNING
    message = "An operator key changed status"
    operation = "operator_key"
    chain_key: str
    key_id: int
    operator_address: str
    previous_status: str
    status: str
    reason: str


class OperatorKeyNonceUsedElsewhere(LogEvent):
    level = logging.ERROR
    message = "An operator key's nonce passed what the allocator handed out: used outside this system, frozen"
    operation = "operator_key"
    chain_key: str
    operator_address: str
    chain_nonce: int
    allocator_next_nonce: int


class KeyOperationRecorded(LogEvent):
    message = "A key operation was recorded (top-up from the treasury, return to it, or the Safe's creation)"
    operation = "operator_key"
    chain_key: str
    operation_id: int
    kind: str
    operator_address: str
    # A top-up's key it fills (the sender's own address, or another key's); a return's or creation's payee.
    recipient_address: str | None
    asset_symbol: str | None
    amount_base_units: int | None


class OperationBroadcastFailed(LogEvent):
    level = logging.WARNING
    message = "Broadcasting a stored attempt failed; it is rebroadcast next pass"
    operation = "settlement_broadcast"
    operation_id: int
    transaction_hash: str


class RefundSettledByOwner(LogEvent):
    level = logging.WARNING
    message = "A needs_owner refund was settled by the owner"
    operation = "settlement_owner"
    operation_id: int
    status: str
    transaction_hash: str | None
    nonce_taken_by_transaction_hash: str | None


class PassBatchReadFailed(LogEvent):
    level = logging.WARNING
    message = "A settlement pass's batched read failed; it reads one request at a time instead"
    operation = "settlement_pass_read"
    # "receipts", "latest_nonces" or "priority_fee"
    read_name: str


class PassSingleReadFailed(LogEvent):
    level = logging.WARNING
    message = "One read of a settlement pass failed; only the operations that need it wait for the next pass"
    operation = "settlement_pass_read"
    read_name: str


class AbandonedNonceFillBroadcast(LogEvent):
    level = logging.WARNING
    message = "A 0-value transfer of an operator key to itself was broadcast at a nonce it had abandoned"
    operation = "operator_key_abandoned_nonce"
    chain_key: str
    operator_address: str
    nonce: int
    transaction_hash: str
    accepting_endpoints: int


class FeeBumpSkippedForRebroadcast(LogEvent):
    level = logging.WARNING
    message = "A fee bump cannot be signed anew; the newest stored attempt is rebroadcast instead"
    operation = "settlement_fee_bump"
    operation_id: int
    reason: str
