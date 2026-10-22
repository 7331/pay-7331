"""What a canonical receipt says a transaction paid an invoice (PLAN.md section 5, step 5): pure functions the
watcher's finality settlement uses."""

from eth_utils import to_checksum_address

from app.infrastructure.evm_rpc_api.schemas import TransactionReceipt
from app.payments.invoices.types import AttributedPayment, UnfinalPayment
from app.payments.settlements.service import TRANSFER_TOPIC


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def find_canonical_payments(
    rows: list[UnfinalPayment], *, receipt: TransactionReceipt | None, block_timestamp_by_number: dict[int, int]
) -> list[AttributedPayment]:
    """What a transaction pays the rows' invoice according to its canonical receipt: the Transfer logs of the
    invoice's token into its deposit, or for a native payment the transaction itself, at the receipt's block and
    that block's header timestamp. Nothing when the receipt is missing (the transaction left the chain) or failed."""
    if receipt is None or not receipt.succeeded:
        return []
    block_timestamp = block_timestamp_by_number[receipt.block_number]
    first_row = rows[0]
    if first_row.token_address is None:
        return [
            AttributedPayment(
                invoice_id=row.invoice_id,
                transaction_hash=receipt.transaction_hash,
                log_index=row.log_index,
                transaction_index=receipt.transaction_index,
                sender=row.sender,
                amount_base_units=row.amount_base_units,
                block_number=receipt.block_number,
                block_hash=receipt.block_hash,
                block_timestamp=block_timestamp,
            )
            for row in rows
            if row.sender is not None
        ]
    payments: list[AttributedPayment] = []
    for entry in receipt.logs:
        if (
            entry.address.lower() != first_row.token_address.lower()
            or len(entry.topics) != 3
            or entry.topics[0] != TRANSFER_TOPIC
            or entry.topics[2] != address_topic(first_row.deposit_address)
        ):
            continue
        amount = int(entry.data, 16) if entry.data not in {"", "0x"} else 0
        if amount == 0:
            continue
        payments.append(
            AttributedPayment(
                invoice_id=first_row.invoice_id,
                transaction_hash=receipt.transaction_hash,
                log_index=entry.log_index,
                transaction_index=receipt.transaction_index,
                sender=to_checksum_address("0x" + entry.topics[1][-40:]),
                amount_base_units=amount,
                block_number=receipt.block_number,
                block_hash=receipt.block_hash,
                block_timestamp=block_timestamp,
            )
        )
    return payments
