"""What the EVM JSON-RPC client returns: parsed, checksummed, integers for quantities."""

from pydantic import BaseModel, ConfigDict


class BlockHead(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    hash: str
    timestamp: int
    base_fee_per_gas: int


class BlockHeads(BaseModel):
    model_config = ConfigDict(frozen=True)

    latest: BlockHead
    finalized: BlockHead


class NativeTransfer(BaseModel):
    """One top-level transaction with a value: who sent how much to whom, and where it sits in the chain."""

    model_config = ConfigDict(frozen=True)

    transaction_hash: str
    sender: str
    recipient: str | None
    value: int
    block_number: int
    block_hash: str
    transaction_index: int


class MinedTransaction(NativeTransfer):
    """A transaction read by its hash, with its sender's nonce: the owner's proof that a nonce went to it."""

    nonce: int


class PendingTransaction(BaseModel):
    """A transaction the endpoint knows by its hash but has not seen in a block (``blockNumber`` null)."""

    model_config = ConfigDict(frozen=True)

    transaction_hash: str


class BlockTransactions(BaseModel):
    model_config = ConfigDict(frozen=True)

    head: BlockHead
    transfers: list[NativeTransfer]


class LogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    address: str
    topics: list[str]
    data: str
    block_number: int
    block_hash: str
    transaction_hash: str
    transaction_index: int
    log_index: int


class TransactionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    transaction_hash: str
    succeeded: bool
    block_number: int
    block_hash: str
    transaction_index: int
    gas_used: int
    effective_gas_price: int
    logs: list[LogEntry]


class Multicall3Call(BaseModel):
    """One call inside ``aggregate3``; ``allow_failure`` is always on, so one failing call never fails the batch."""

    model_config = ConfigDict(frozen=True)

    target: str
    call_data: bytes


class Multicall3Result(BaseModel):
    model_config = ConfigDict(frozen=True)

    succeeded: bool
    return_data: bytes
