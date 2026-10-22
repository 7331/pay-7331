"""Native attribution: which transaction sent the native coin that raised a watched deposit's balance (PLAN.md section 5,
step 4). Split from ``service.py`` (2026-10-14) to keep it under 800 lines."""

from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.errors import EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, BlockTransactions
from app.payments.chain_watcher.types import NativeAttribution, NativeRiseSelection
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoices.types import (
    AttributedPayment,
    AttributedStrayPayment,
    DepositBalances,
    UnfinalPayment,
    WatchedDeposit,
)

# Native attribution reads at most this far back for one rise (200 Polygon blocks, 1,600 Arbitrum blocks); older history
# becomes an unknown sender. Every "about N blocks" of the watcher is a time, turned into the chain's blocks with its
# `seconds_per_block` (PLAN.md section 15).
NATIVE_ATTRIBUTION_MAXIMUM_SECONDS = 400


async def attribute_native_payments(
    endpoint: EvmRpcEndpoint,
    *,
    chain: EvmChainConfiguration,
    watched: list[WatchedDeposit],
    balances: list[DepositBalances],
    block_heads: BlockHeads,
    last_finalized: int | None,
    unfinal_payments: list[UnfinalPayment],
) -> NativeAttribution:
    """Native rises: fetch only the blocks since each deposit's last reading and match ``tx.to``. A sweep at
    ``latest`` can hide a payment's rise there (0.3 swept, 0.3 paid: the balance reads the same), so a deposit
    whose finalized gross rose past what was credited also gets the blocks finalized since the last tick
    (2026-10-09, live: a third-party sweep then a top-up left the top-up with no payment row). A token invoice's
    native rise is stray money (INC-001): its transactions become stray rows, and what they leave unexplained a
    row with no sender at ``latest``.

    Each range reads at most ``NATIVE_ATTRIBUTION_MAXIMUM_SECONDS`` of blocks: the rise at ``latest`` back from the
    head, the finalized one (``unfinal_payments`` do not explain it) back from the finalized head, up to which the
    missing payment was mined. Where ``finalized`` trails by 15 minutes (Arbitrum) the two are far apart, and a
    finalized rise a recorded payment row already explains reads no block at all (PLAN.md section 15)."""
    maximum_blocks = chain.blocks_in_seconds(NATIVE_ATTRIBUTION_MAXIMUM_SECONDS)
    balance_by_invoice = {balance.invoice_id: balance for balance in balances}
    latest_number = block_heads.latest.number
    finalized_number = block_heads.finalized.number
    oldest_block = latest_number - maximum_blocks + 1
    oldest_finalized_block = finalized_number - maximum_blocks + 1
    native_symbol = chain.native_asset.symbol
    selection = select_deposits_with_native_rises(
        watched,
        balance_by_invoice=balance_by_invoice,
        explained_by_invoice=sum_unfinal_native_payments_by_invoice(unfinal_payments, native_symbol=native_symbol),
        last_finalized=last_finalized,
        oldest_block=oldest_block,
        oldest_finalized_block=oldest_finalized_block,
    )
    if not selection.invoice_by_deposit:
        return NativeAttribution(payments=[], stray_payments=[])
    block_numbers_to_read = list_native_attribution_block_numbers(
        selection,
        oldest_block=oldest_block,
        latest_number=latest_number,
        oldest_finalized_block=oldest_finalized_block,
        finalized_number=finalized_number,
    )
    transfers = collect_native_transfers_into_selected_deposits(
        await endpoint.read_blocks_with_transactions(block_numbers_to_read),
        invoice_by_deposit=selection.invoice_by_deposit,
    )
    transfers = await keep_succeeded_native_transfers(endpoint, transfers=transfers)
    stray_deposit_by_invoice = selection.stray_deposit_by_invoice
    payments = [transfer for transfer in transfers if transfer.invoice_id not in stray_deposit_by_invoice]
    stray_payments = [
        AttributedStrayPayment(asset_symbol=native_symbol, **transfer.model_dump())
        for transfer in transfers
        if transfer.invoice_id in stray_deposit_by_invoice
    ]
    stray_payments = add_unexplained_stray_native_rises(
        stray_payments,
        stray_deposit_by_invoice=stray_deposit_by_invoice,
        balance_by_invoice=balance_by_invoice,
        native_symbol=native_symbol,
        oldest_block=oldest_block,
        block_heads=block_heads,
    )
    return NativeAttribution(payments=payments, stray_payments=stray_payments)


def sum_unfinal_native_payments_by_invoice(
    unfinal_payments: list[UnfinalPayment], *, native_symbol: str
) -> dict[str, int]:
    """Per invoice, the native base units its recorded payment rows not final yet already explain."""
    explained_by_invoice: dict[str, int] = {}
    for unfinal_payment in unfinal_payments:
        if unfinal_payment.asset_symbol == native_symbol:
            explained_by_invoice[unfinal_payment.invoice_id] = (
                explained_by_invoice.get(unfinal_payment.invoice_id, 0) + unfinal_payment.amount_base_units
            )
    return explained_by_invoice


def select_deposits_with_native_rises(
    watched: list[WatchedDeposit],
    *,
    balance_by_invoice: dict[str, DepositBalances],
    explained_by_invoice: dict[str, int],
    last_finalized: int | None,
    oldest_block: int,
    oldest_finalized_block: int,
) -> NativeRiseSelection:
    """The deposits whose native balance rose at ``latest`` (a token invoice's as stray money), or whose finalized gross
    rose past what was credited and what unfinal payments explain, with the block each range starts from."""
    invoice_by_deposit: dict[str, str] = {}
    stray_deposit_by_invoice: dict[str, WatchedDeposit] = {}
    start_blocks: list[int] = []
    finalized_start_blocks: list[int] = []
    for deposit in watched:
        balance = balance_by_invoice.get(deposit.invoice_id)
        if balance is None:
            continue
        if deposit.token_address is not None:
            if (
                balance.latest_native_balance_base_units is not None
                and deposit.latest_native_balance_base_units is not None
                and balance.latest_native_balance_base_units > deposit.latest_native_balance_base_units
            ):
                start_blocks.append((deposit.latest_reading_block_number or oldest_block) + 1)
                invoice_by_deposit[deposit.deposit_address] = deposit.invoice_id
                stray_deposit_by_invoice[deposit.invoice_id] = deposit
            continue
        if balance.latest_balance_base_units > deposit.latest_balance_base_units:
            start_blocks.append((deposit.latest_reading_block_number or oldest_block) + 1)
            invoice_by_deposit[deposit.deposit_address] = deposit.invoice_id
        unexplained_finalized_rise = (
            balance.finalized_balance_base_units
            + deposit.swept_base_units
            - deposit.received_base_units
            - explained_by_invoice.get(deposit.invoice_id, 0)
        )
        if unexplained_finalized_rise > 0:
            finalized_start_blocks.append(
                (last_finalized if last_finalized is not None else oldest_finalized_block) + 1
            )
            invoice_by_deposit[deposit.deposit_address] = deposit.invoice_id
    return NativeRiseSelection(
        invoice_by_deposit=invoice_by_deposit,
        stray_deposit_by_invoice=stray_deposit_by_invoice,
        start_blocks=start_blocks,
        finalized_start_blocks=finalized_start_blocks,
    )


def list_native_attribution_block_numbers(
    selection: NativeRiseSelection,
    *,
    oldest_block: int,
    latest_number: int,
    oldest_finalized_block: int,
    finalized_number: int,
) -> list[int]:
    """The blocks to read, ascending: from the earliest start back from ``latest``, and from the earliest finalized
    start back from the finalized head, each at most ``NATIVE_ATTRIBUTION_MAXIMUM_SECONDS`` of blocks."""
    block_numbers_to_read: set[int] = set()
    if selection.start_blocks:
        block_numbers_to_read.update(range(max(oldest_block, min(selection.start_blocks)), latest_number + 1))
    if selection.finalized_start_blocks:
        block_numbers_to_read.update(
            range(max(oldest_finalized_block, min(selection.finalized_start_blocks)), finalized_number + 1)
        )
    return sorted(block_numbers_to_read)


def collect_native_transfers_into_selected_deposits(
    blocks: list[BlockTransactions], *, invoice_by_deposit: dict[str, str]
) -> list[AttributedPayment]:
    """Every native transfer in the blocks whose ``tx.to`` is a selected deposit, in block order."""
    transfers: list[AttributedPayment] = []
    for block in blocks:
        for transfer in block.transfers:
            invoice_id = invoice_by_deposit.get(transfer.recipient or "")
            if invoice_id is None:
                continue
            transfers.append(
                AttributedPayment(
                    invoice_id=invoice_id,
                    transaction_hash=transfer.transaction_hash,
                    log_index=-1,
                    transaction_index=transfer.transaction_index,
                    sender=transfer.sender,
                    amount_base_units=transfer.value,
                    block_number=transfer.block_number,
                    block_hash=transfer.block_hash,
                    block_timestamp=block.head.timestamp,
                )
            )
    return transfers


async def keep_succeeded_native_transfers(
    endpoint: EvmRpcEndpoint, *, transfers: list[AttributedPayment]
) -> list[AttributedPayment]:
    """The transfers whose transaction succeeded; no read when there are none."""
    if not transfers:
        return transfers
    # A reverted transaction moved nothing but still names a sender and a value: it must not become a payment
    # (it would claim another sender's refundable share, 2026-10-09).
    receipts = await endpoint.read_transaction_receipts([transfer.transaction_hash for transfer in transfers])
    if any(receipt is None for receipt in receipts):
        raise EvmRpcUnavailableError("a fetched block's transaction has no receipt at this endpoint")
    return [
        transfer
        for transfer, receipt in zip(transfers, receipts, strict=True)
        if receipt is not None and receipt.succeeded
    ]


def add_unexplained_stray_native_rises(
    stray_payments: list[AttributedStrayPayment],
    *,
    stray_deposit_by_invoice: dict[str, WatchedDeposit],
    balance_by_invoice: dict[str, DepositBalances],
    native_symbol: str,
    oldest_block: int,
    block_heads: BlockHeads,
) -> list[AttributedStrayPayment]:
    """The stray payments, plus one row with no sender at ``latest`` for each token invoice's native rise its stray
    transactions since the last reading leave unexplained."""
    stray_payments = list(stray_payments)
    for invoice_id, deposit in stray_deposit_by_invoice.items():
        latest_native_balance = balance_by_invoice[invoice_id].latest_native_balance_base_units or 0
        rise = latest_native_balance - (deposit.latest_native_balance_base_units or 0)
        since_block = deposit.latest_reading_block_number or oldest_block
        explained = sum(
            stray.amount_base_units
            for stray in stray_payments
            if stray.invoice_id == invoice_id and stray.block_number > since_block
        )
        if rise > explained:
            # A contract's internal transfer (or one older than the blocks read): no transaction, no sender.
            stray_payments.append(
                AttributedStrayPayment(
                    invoice_id=invoice_id,
                    asset_symbol=native_symbol,
                    transaction_hash=None,
                    log_index=-1,
                    transaction_index=0,
                    sender=None,
                    amount_base_units=rise - explained,
                    block_number=block_heads.latest.number,
                    block_hash=block_heads.latest.hash,
                    block_timestamp=block_heads.latest.timestamp,
                )
            )
    return stray_payments
