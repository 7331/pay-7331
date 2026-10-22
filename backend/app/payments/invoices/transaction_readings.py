"""One transaction hash as the chain shows it, read once and shared by the operator's
``record-stray-payment-from-transaction`` (INC-001) and the payer's "I paid" check.

Every endpoint of the chain is asked in turn until one knows the hash (a public node may not have seen another's
pending transaction). On the endpoint that knows it: ``eth_getTransactionByHash`` (null when unknown, ``blockNumber``
null while pending), ``eth_getTransactionReceipt`` (``status`` 1 success, 0 failure), both heads and the receipt's
block header, so finality is judged on one node's view
(https://ethereum.org/en/developers/docs/apis/json-rpc/#eth_gettransactionbyhash and #eth_gettransactionreceipt).
A transaction is final when its block is at or below the finalized head and that height still holds its block.
"""

import re

from eth_utils import to_checksum_address

from app.infrastructure.evm_rpc_api.client import EvmRpcClient, EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.schemas import LogEntry, MinedTransaction, PendingTransaction
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoices.enums import ChainTransactionState
from app.payments.invoices.types import ChainTransactionReading, TransactionTransfer
from app.payments.settlements.service import TRANSFER_TOPIC

# An EVM transaction hash: 0x and 32 bytes in hex. A Tron txid is the same 64 hex digits without 0x; Solana's are
# base58 and Bitcoin's 64 hex without 0x, so none of them pass.
EVM_TRANSACTION_HASH_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}")
# ERC-20 Transfer(address indexed from, address indexed to, uint256 value): three topics. ERC-721's has four.
ERC20_TRANSFER_TOPIC_COUNT = 3


def normalize_transaction_hash(text: str) -> str | None:
    """The hash in lowercase, or None when the text is not an EVM transaction hash."""
    candidate = text.strip()
    if EVM_TRANSACTION_HASH_PATTERN.fullmatch(candidate) is None:
        return None
    return candidate.lower()


async def read_transaction_on_endpoint(
    endpoint: EvmRpcEndpoint, *, transaction_hash: str, chain: EvmChainConfiguration
) -> ChainTransactionReading | None:
    """What one endpoint shows of the hash; None when it does not know it, so the next endpoint is asked."""
    transaction = await endpoint.read_transaction_by_hash(transaction_hash)
    if transaction is None:
        return None
    if isinstance(transaction, PendingTransaction):
        return ChainTransactionReading(transaction_hash=transaction_hash, state=ChainTransactionState.NOT_MINED)
    (receipt,) = await endpoint.read_transaction_receipts([transaction_hash])
    if receipt is None:
        # Mined a moment ago and the receipt not indexed yet: as good as not mined for the payer.
        return ChainTransactionReading(transaction_hash=transaction_hash, state=ChainTransactionState.NOT_MINED)
    if not receipt.succeeded:
        return ChainTransactionReading(transaction_hash=receipt.transaction_hash, state=ChainTransactionState.FAILED)
    block_heads = await endpoint.read_block_heads()
    (block,) = await endpoint.read_block_heads_by_number([receipt.block_number])
    return ChainTransactionReading(
        transaction_hash=receipt.transaction_hash,
        state=ChainTransactionState.SUCCEEDED,
        final=receipt.block_number <= block_heads.finalized.number and block.hash == receipt.block_hash,
        block_number=receipt.block_number,
        block_hash=receipt.block_hash,
        block_timestamp=block.timestamp,
        transaction_index=receipt.transaction_index,
        finalized_block_number=block_heads.finalized.number,
        transfers=list_transaction_transfers(transaction, logs=receipt.logs, chain=chain),
    )


def list_transaction_transfers(
    transaction: MinedTransaction, *, logs: list[LogEntry], chain: EvmChainConfiguration
) -> list[TransactionTransfer]:
    """The transaction's own native value, then every ERC-20 Transfer log with a value, listed token or not. A
    contract's internal native transfers do not show here (no trace API on public endpoints)."""
    transfers: list[TransactionTransfer] = []
    if transaction.recipient is not None and transaction.value > 0:
        transfers.append(
            TransactionTransfer(
                recipient=transaction.recipient,
                sender=transaction.sender,
                asset_symbol=chain.native_asset.symbol,
                token_address=None,
                log_index=-1,
                amount_base_units=transaction.value,
            )
        )
    symbol_by_token = {
        asset.token_address.lower(): asset.symbol for asset in chain.assets if asset.token_address is not None
    }
    for entry in logs:
        if len(entry.topics) != ERC20_TRANSFER_TOPIC_COUNT or entry.topics[0] != TRANSFER_TOPIC:
            continue
        amount = int(entry.data, 16) if entry.data not in {"", "0x"} else 0
        if amount == 0:
            continue
        transfers.append(
            TransactionTransfer(
                recipient=to_checksum_address("0x" + entry.topics[2][-40:]),
                sender=to_checksum_address("0x" + entry.topics[1][-40:]),
                asset_symbol=symbol_by_token.get(entry.address.lower()),
                token_address=entry.address,
                log_index=entry.log_index,
                amount_base_units=amount,
            )
        )
    return transfers


async def read_chain_transaction(
    transaction_hash: str, *, rpc_client: EvmRpcClient, chain: EvmChainConfiguration
) -> ChainTransactionReading:
    """The hash on the first endpoint that knows it; ``unknown`` when none does. Raises ``EvmRpcUnavailableError``
    only when no endpoint answered at all."""
    reading = await rpc_client.call_on_each_endpoint_until_found(
        lambda endpoint: read_transaction_on_endpoint(endpoint, transaction_hash=transaction_hash, chain=chain)
    )
    if reading is None:
        return ChainTransactionReading(transaction_hash=transaction_hash, state=ChainTransactionState.UNKNOWN)
    return reading
