"""ChainWatcherService: one chain's watcher tick (PLAN.md section 5), every ``poll_interval_seconds``.

A tick runs against one endpoint (``EvmRpcClient.endpoint_for_tick``); a failure anywhere fails the tick and the next
one fails over. In order:

1. ``latest`` and ``finalized`` heads; a finalized number below the last applied one skips the tick.
2. ``DepositSwept`` logs of the factory up to the finalized block (ranges of ``LOG_RANGE_BLOCKS``), stored once.
   Until they reach the finalized block, balances are not applied (gross received would be short). A log read the
   endpoint refuses for its range or result count is read again over half the range, kept while ticks stay on that
   endpoint, down to ``MINIMUM_LOG_RANGE_BLOCKS``; a refusal there, or one for history (logs too far back for a
   keyless caller), fails the tick, so the next fails over. Token ``Transfer`` reads (step 3) do the same. The last
   ``FINALIZED_REREAD_SECONDS`` of finalized blocks are read again every tick, both streams.
3. Token ``Transfer`` logs of every configured token into watched deposits from the last applied finalized block
   up to ``latest`` (attribution of token payments; a transfer in another token than the invoice's is a stray
   payment, INC-001; the unfinal range is read again every tick, so a reorged range is re-read, at most
   ``UNFINAL_TRANSFER_REREAD_MAXIMUM_SECONDS`` back on a chain whose ``finalized`` trails far behind, Arbitrum's 15
   minutes). A range's filters,
   one per ``DEPOSITS_PER_LOG_FILTER`` deposits, run ``CONCURRENT_TRANSFER_LOG_READS`` at a time. A refusal of a
   ``toBlock`` past the answering node's head, for a range above the finalized block, stops the reads at the
   finalized block for the tick instead of failing it.
4. One Multicall3 ``aggregate3`` pinned to the finalized block hash (EIP-1898) and one at ``latest`` for every
   watched deposit's balance, the latter with a token invoice's native balance too (stray native money, INC-001);
   native rises at ``latest`` fetch only the blocks since the last reading and match ``tx.to`` (attribution of
   native payments and native stray payments; a rise no transaction explains becomes an unknown sender's, or for a
   stray rise a row with no sender).
5. Payments and stray payments whose block is now final are rechecked against the canonical block hash, per
   transaction; a moved one is rebuilt from its receipt (one transfer, one row, at its canonical block and log
   index).
6. The readings go to ``ChainReadingsService`` in one transaction with expiry and closing.
7. The rescan (``DepositRescanService``): every deposit ever made, in every configured asset, at the finalized block,
   in two tiers: invoices made within ``recent_deposit_window_seconds`` or not closed yet every
   ``rescan_interval_seconds``, every other deposit every ``old_deposit_rescan_interval_seconds``. Each tier's pass
   reads a bounded slice a tick, paced to end in about half its interval; the recent tier has priority over the
   tick's budget of balance calls and the old tier reads what is left.
   Then one slice of each tier of the static deposit addresses' scan (``StaticDepositScanService``, PLAN.md section
   17): every static address in every configured coin at the finalized block, bounded slices a tick; a hot tier (made
   or paid within a day) a pass every minute, a cold tier every 20 minutes, sharing one balance call budget.
8. ``SettlementRunner`` moves sweeps and refunds one step; each outcome is applied to its invoice in the transaction
   that resolves the operation (``ChainReadingsService.apply_settlement_outcomes``, injected by the composition).
9. Automatic refunds deferred for want of a price are decided again (one indexed query; nothing while no feed
   answers). Last, in its own transaction, so a failure there never holds back the readings or the settlements.

After a completed tick, ``OperatorAlertService`` writes the heartbeat and, once a minute, alerts the operator.
"""

import asyncio
from collections.abc import Callable
from itertools import batched

from eth_utils import to_checksum_address
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.deposit_factory_api.client import DepositFactoryApiClient
from app.infrastructure.evm_rpc_api.client import (
    EvmRpcClient,
    EvmRpcEndpoint,
    decode_uint256,
    encode_native_balance_call,
    encode_token_balance_call,
)
from app.infrastructure.evm_rpc_api.errors import (
    EvmLogBeyondHeadRefusedError,
    EvmLogRangeRefusedError,
)
from app.infrastructure.evm_rpc_api.schemas import (
    BlockHead,
    BlockHeads,
    LogEntry,
    Multicall3Call,
    TransactionReceipt,
)
from app.payments.chain_watcher.canonical_payments import address_topic, find_canonical_payments
from app.payments.chain_watcher.log_events import (
    FinalizedHeadWentBack,
    LogRangeNarrowed,
    TransferLogsStoppedAtFinalized,
    WatcherCatchingUp,
)
from app.payments.chain_watcher.models import CursorStream
from app.payments.chain_watcher.native_attribution import attribute_native_payments
from app.payments.chain_watcher.operator_alerts_service import OperatorAlertService
from app.payments.chain_watcher.repository import ChainCursorRepository
from app.payments.chain_watcher.rescan_service import DepositRescanService
from app.payments.chain_watcher.static_deposit_scan_service import StaticDepositScanService
from app.payments.chain_watcher.types import ResolvedFinality
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoices.chain_readings_service import ChainReadingsService
from app.payments.invoices.types import (
    AttributedPayment,
    AttributedStrayPayment,
    CanonicalTransactionPayments,
    DepositBalances,
    FinalPaymentBlock,
    UnfinalPayment,
    WatchedDeposit,
)
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.settlements.runner_service import SettlementRunner
from app.payments.settlements.service import TRANSFER_TOPIC
from app.shared import logging as log
from app.shared.logging import LogContext, bound_log_context

logger = log.get_logger(__name__)

# The widest log read an endpoint starts with. The endpoints' own caps differ (chains.toml, measured 2026-10-12:
# dRPC's free plan 101 blocks, Tenderly and QuickNode 10,000), so a refused range is halved while ticks stay on that
# endpoint (`narrow_log_range_after_refusal`; it doubles back after LOG_RANGE_WIDEN_AFTER_CLEAN_READS clean reads),
# down to MINIMUM_LOG_RANGE_BLOCKS; a refusal at or under it fails the tick over to an endpoint that takes wider
# ranges. At the tip a tick reads a few blocks, so a refusal there fails over at once, as any failed read did before
# 2026-10-12.
LOG_RANGE_BLOCKS = 2_000
# Five ranges of 100 blocks a tick still outrun Polygon's 2 s blocks many times over; an endpoint that cannot serve
# 100 would make a catch-up crawl, so the next one is tried.
MINIMUM_LOG_RANGE_BLOCKS = 100
LOG_RANGES_PER_TICK = 5
DEPOSITS_PER_LOG_FILTER = 100
TICK_TIMEOUT_SECONDS = 90
# Transfer log reads in flight at once for one range, one per chunk of DEPOSITS_PER_LOG_FILTER deposits: about 2,100
# open invoices make 21 filters a range, read one after another before 2026-10-13. Kept small because the free public
# endpoints rate limit per IP (dRPC: 120,000 compute units a minute, chains.toml) and a refusal fails the tick over.
CONCURRENT_TRANSFER_LOG_READS = 4
# A narrowed log range doubles back towards LOG_RANGE_BLOCKS after this many reads in a row pass, so one timeout (counted
# as a range refusal) does not keep a catch-up at 100 blocks for as long as ticks stay on the endpoint (2026-10-13).
LOG_RANGE_WIDEN_AFTER_CLEAN_READS = 50
# Every "about N blocks" of the watcher is a time, turned into the chain's blocks with its `seconds_per_block`
# (PLAN.md section 15): 2 s Polygon blocks and 0.25 s Arbitrum blocks mean the same span.
# Both log streams read the last this many seconds of finalized blocks again every tick (2026-10-13). dRPC and Pocket
# answered a read near the tip with fewer logs than were there and no error (and dRPC clamped a `toBlock` past its
# head), which the spec forbids ("Clients MUST NOT clamp the range to the head block or return a partial result",
# https://github.com/ethereum/execution-apis, src/eth/filter.yaml), and nothing else rereads a final block's
# DepositSwept or Transfer logs. 64 s is 32 Polygon blocks past finality (2-5 s behind `latest`,
# https://docs.polygon.technology/pos/concepts/finality/finality/), so a read reaches back about 35 blocks, inside
# publicnode's 98-block log window (chains.toml). Rereading is harmless: payments, stray payments and DepositSwept
# events are inserted once per transaction and log index, and only a new row has an effect (an event, a refund).
FINALIZED_REREAD_SECONDS = 64
# The transfer reads start from the last finalized block, so a reorg that re-mines a transfer is read again; where
# `finalized` trails `latest` by 15 minutes (Arbitrum, about 4,000 blocks) that would reread them all every tick and,
# on an endpoint that takes 100 blocks a read, never reach `latest`. The start is therefore at most this far behind the
# transfer cursor (60 Polygon blocks, past its 2 to 5 s finality). A transfer a deeper reorg mines behind the cursor is
# found by the rescan at the finalized block as money with no sender, never lost (PLAN.md section 15).
UNFINAL_TRANSFER_REREAD_MAXIMUM_SECONDS = 120

type SessionFactory = Callable[[], AsyncSession]


def select_watched_token_transfer_entries(
    log_entries_per_chunk: list[list[LogEntry]],
    *,
    invoice_and_own_token_by_deposit: dict[str, tuple[str, str | None]],
    asset_symbol_by_token: dict[str, str],
) -> list[tuple[str, int, LogEntry]]:
    """The Transfer log entries of a configured token into a watched deposit with a nonzero amount, each with its
    invoice id and amount, in the order read."""
    transfer_entries: list[tuple[str, int, LogEntry]] = []
    for log_entries in log_entries_per_chunk:
        for entry in log_entries:
            recipient = ("0x" + entry.topics[2][-40:]).lower()
            invoice_and_own_token = invoice_and_own_token_by_deposit.get(recipient)
            amount = int(entry.data, 16) if entry.data not in {"", "0x"} else 0
            if invoice_and_own_token is None or entry.address.lower() not in asset_symbol_by_token or amount == 0:
                continue
            transfer_entries.append((invoice_and_own_token[0], amount, entry))
    return transfer_entries


def split_token_transfers_into_payments_and_stray_payments(
    transfer_entries: list[tuple[str, int, LogEntry]],
    *,
    timestamp_by_block_number: dict[int, int],
    invoice_and_own_token_by_deposit: dict[str, tuple[str, str | None]],
    asset_symbol_by_token: dict[str, str],
) -> tuple[list[AttributedPayment], list[AttributedStrayPayment]]:
    """A transfer in the deposit's own token is a payment, one in any other configured token a stray payment
    (INC-001)."""
    payments: list[AttributedPayment] = []
    stray_payments: list[AttributedStrayPayment] = []
    for invoice_id, amount, entry in transfer_entries:
        transfer = AttributedPayment(
            invoice_id=invoice_id,
            transaction_hash=entry.transaction_hash,
            log_index=entry.log_index,
            transaction_index=entry.transaction_index,
            sender=to_checksum_address("0x" + entry.topics[1][-40:]),
            amount_base_units=amount,
            block_number=entry.block_number,
            block_hash=entry.block_hash,
            block_timestamp=timestamp_by_block_number[entry.block_number],
        )
        recipient = ("0x" + entry.topics[2][-40:]).lower()
        if entry.address.lower() == invoice_and_own_token_by_deposit[recipient][1]:
            payments.append(transfer)
        else:
            stray_payments.append(
                AttributedStrayPayment(
                    asset_symbol=asset_symbol_by_token[entry.address.lower()], **transfer.model_dump()
                )
            )
    return payments, stray_payments


class ChainWatcherService:
    def __init__(
        self,
        *,
        chain: EvmChainConfiguration,
        rpc_client: EvmRpcClient,
        factory_client: DepositFactoryApiClient,
        settlement_runner: SettlementRunner,
        session_factory: SessionFactory,
        redis: Redis,
        wiring: InvoiceWiring,
        operator_alert_service: OperatorAlertService,
        deposit_rescan_service: DepositRescanService,
        static_deposit_scan_service: StaticDepositScanService,
    ) -> None:
        self.chain = chain
        self.static_deposit_scan_service = static_deposit_scan_service
        self.operator_alert_service = operator_alert_service
        self.deposit_rescan_service = deposit_rescan_service
        self.rpc_client = rpc_client
        self.factory_client = factory_client
        self.settlement_runner = settlement_runner
        self.session_factory = session_factory
        self.redis = redis
        self.wiring = wiring
        # The log range narrowed on `log_range_rpc_url`, kept while ticks stay on that endpoint so a catch-up does not
        # rediscover its cap every tick (dRPC: 5 refused reads a tick, 2026-10-12); reset when the endpoint changes.
        self.log_range_blocks = LOG_RANGE_BLOCKS
        self.log_range_rpc_url: str | None = None
        self.clean_log_reads_since_narrowed = 0
        self.reread_blocks = chain.blocks_in_seconds(FINALIZED_REREAD_SECONDS)

    def readings_on(self, session: AsyncSession) -> ChainReadingsService:
        return ChainReadingsService.on_session(session, redis=self.redis, wiring=self.wiring)

    async def run_tick(self) -> bool:
        """One tick; False when more work waits (the loop then starts the next at once)."""
        endpoint = self.rpc_client.endpoint_for_tick()
        with bound_log_context(LogContext(chain_key=self.chain.key)):
            try:
                async with asyncio.timeout(TICK_TIMEOUT_SECONDS):
                    caught_up = await self.run_tick_on(endpoint)
            except Exception:
                # The process loop logs the failure with its traceback; the next tick uses the next endpoint.
                self.rpc_client.fail_over()
                raise
            await self.operator_alert_service.after_completed_tick(
                static_deposit_scan_findings=self.static_deposit_scan_service.find_slow_pass_findings()
            )
            return caught_up

    async def run_tick_on(self, endpoint: EvmRpcEndpoint) -> bool:
        if endpoint.rpc_url != self.log_range_rpc_url:
            self.log_range_rpc_url = endpoint.rpc_url
            self.log_range_blocks = LOG_RANGE_BLOCKS
            self.clean_log_reads_since_narrowed = 0
        block_heads = await endpoint.read_block_heads()
        cursors = await self.read_or_start_cursors(block_heads)
        last_finalized = cursors.get(CursorStream.FINALIZED_HEAD)
        if last_finalized is not None and block_heads.finalized.number < last_finalized:
            log.emit(
                logger,
                event=FinalizedHeadWentBack(
                    chain_key=self.chain.key,
                    finalized_block_number=block_heads.finalized.number,
                    last_applied_block_number=last_finalized,
                ),
            )
            return True
        swept_caught_up = await self.ingest_deposit_swept_logs(
            endpoint,
            last_block_number=cursors[CursorStream.DEPOSIT_SWEPT_LOGS],
            target_block_number=block_heads.finalized.number,
        )
        async with self.session_factory() as session:
            watched = await self.readings_on(session).list_watched_deposits(self.chain.key)
        # Transfer logs are read again from the last applied finalized block on: a reorg can re-mine a transfer (or
        # mine a new one) in a block the cursor already passed, and each block is then read once more while final
        # (2026-10-09). On Polygon that is the 2-3 blocks finality trails `latest` by, on Arbitrum at most
        # UNFINAL_TRANSFER_REREAD_MAXIMUM_SECONDS of the 15 minutes. The last FINALIZED_REREAD_SECONDS of finalized
        # blocks are read again on top (2026-10-13).
        token_cursor = cursors[CursorStream.TOKEN_TRANSFER_LOGS]
        transfers_caught_up = await self.ingest_token_transfers(
            endpoint,
            watched=watched,
            scan_cursor_block_number=token_cursor,
            last_block_number=max(
                min(
                    token_cursor,
                    last_finalized if last_finalized is not None else token_cursor,
                    block_heads.finalized.number,
                ),
                token_cursor - self.chain.blocks_in_seconds(UNFINAL_TRANSFER_REREAD_MAXIMUM_SECONDS),
            ),
            target_block_number=block_heads.latest.number,
            finalized_block_number=block_heads.finalized.number,
        )
        if not swept_caught_up:
            return False
        balances = await self.read_balances(endpoint, watched=watched, block_heads=block_heads)
        async with self.session_factory() as session:
            unfinal_payments = await self.readings_on(session).list_unfinal_payments(
                self.chain.key, up_to_block_number=block_heads.finalized.number
            )
        native_attribution = await attribute_native_payments(
            endpoint,
            chain=self.chain,
            watched=watched,
            balances=balances,
            block_heads=block_heads,
            last_finalized=last_finalized,
            unfinal_payments=unfinal_payments,
        )
        await self.settle_payment_finality(endpoint, block_heads=block_heads, unfinal=unfinal_payments)
        senders_with_code = await self.find_senders_with_code(
            endpoint,
            senders=[payment.sender for payment in native_attribution.payments]
            + [stray.sender for stray in native_attribution.stray_payments if stray.sender is not None],
        )
        async with self.session_factory() as session, session.begin():
            readings = self.readings_on(session)
            await readings.record_payments(
                native_attribution.payments,
                senders_with_code=senders_with_code,
                finalized_block_number=block_heads.finalized.number,
            )
            await readings.record_stray_payments(
                native_attribution.stray_payments,
                senders_with_code=senders_with_code,
                finalized_block_number=block_heads.finalized.number,
            )
            await readings.apply_balances(
                self.chain.key, balances=balances, finalized=block_heads.finalized, latest=block_heads.latest
            )
            await readings.expire_and_close_invoices(self.chain.key)
            await ChainCursorRepository(session=session).write_cursor(
                self.chain.key, stream_name=CursorStream.FINALIZED_HEAD, last_block_number=block_heads.finalized.number
            )
        await self.deposit_rescan_service.rescan_when_due(endpoint, block_heads=block_heads)
        await self.static_deposit_scan_service.scan_when_due(endpoint, block_heads=block_heads)
        await self.operator_alert_service.maintain_operator_keys(endpoint, block_heads=block_heads)
        await self.settlement_runner.advance_operations(self.chain.key, endpoint=endpoint, block_heads=block_heads)
        async with self.session_factory() as session, session.begin():
            await self.readings_on(session).redecide_deferred_automatic_refunds(self.chain.key)
        return transfers_caught_up

    async def read_or_start_cursors(self, block_heads: BlockHeads) -> dict[CursorStream, int]:
        """The log cursors; on a chain's first tick they start at the current heads (no invoice exists before)."""
        async with self.session_factory() as session, session.begin():
            repository = ChainCursorRepository(session=session)
            cursors = await repository.read_cursors(self.chain.key)
            starting_points = {
                CursorStream.DEPOSIT_SWEPT_LOGS: block_heads.finalized.number,
                CursorStream.TOKEN_TRANSFER_LOGS: block_heads.latest.number,
            }
            for stream_name, block_number in starting_points.items():
                if stream_name not in cursors:
                    await repository.write_cursor(
                        self.chain.key, stream_name=stream_name, last_block_number=block_number
                    )
                    cursors[stream_name] = block_number
        return cursors

    async def ingest_deposit_swept_logs(
        self, endpoint: EvmRpcEndpoint, *, last_block_number: int, target_block_number: int
    ) -> bool:
        """Read DepositSwept up to the finalized block, a few ranges per tick, the last
        ``FINALIZED_REREAD_SECONDS`` of them again; True once caught up."""
        # Back from the cursor, not the head: a tick that ran late (finalized moved past the reread) still reads its
        # predecessor's last blocks again.
        from_block = max(min(last_block_number, target_block_number) - self.reread_blocks, -1) + 1
        ranges_read = 0
        while ranges_read < LOG_RANGES_PER_TICK and from_block <= target_block_number:
            to_block = min(target_block_number, from_block + self.log_range_blocks - 1)
            try:
                events = await self.factory_client.read_deposit_swept_events(
                    endpoint, from_block=from_block, to_block=to_block
                )
            except EvmLogRangeRefusedError as refusal:
                self.narrow_log_range_after_refusal(
                    refusal, stream_name=CursorStream.DEPOSIT_SWEPT_LOGS, from_block=from_block, to_block=to_block
                )
                continue
            self.widen_log_range_after_clean_read()
            async with self.session_factory() as session, session.begin():
                await self.readings_on(session).record_deposit_sweep_events(self.chain.key, events=events)
                await ChainCursorRepository(session=session).write_cursor(
                    self.chain.key, stream_name=CursorStream.DEPOSIT_SWEPT_LOGS, last_block_number=to_block
                )
            from_block = to_block + 1
            ranges_read += 1
        if from_block <= target_block_number:
            log.emit(
                logger,
                event=WatcherCatchingUp(
                    chain_key=self.chain.key,
                    stream_name=CursorStream.DEPOSIT_SWEPT_LOGS.value,
                    last_block_number=from_block - 1,
                    target_block_number=target_block_number,
                ),
            )
            return False
        return True

    async def ingest_token_transfers(
        self,
        endpoint: EvmRpcEndpoint,
        *,
        watched: list[WatchedDeposit],
        scan_cursor_block_number: int,
        last_block_number: int,
        target_block_number: int,
        finalized_block_number: int,
    ) -> bool:
        """Token money into watched deposits from Transfer logs up to ``latest``, the last
        ``FINALIZED_REREAD_SECONDS`` of finalized blocks again; True once caught up. A ``toBlock`` refused as past
        the answering node's head, in a range above ``finalized_block_number``, ends this tick's reads at the finalized
        block: the blocks above it are read again next tick anyway (2026-10-13)."""
        # One filter per chunk of deposits names every configured token, so the log reads per tick do not grow with
        # the asset count (10 on Polygon, 2026-10-11); a transfer in the deposit's own token is a payment, one in any
        # other configured token a stray payment that never counts (INC-001, 2026-10-11).
        invoice_and_own_token_by_deposit: dict[str, tuple[str, str | None]] = {
            deposit.deposit_address.lower(): (
                deposit.invoice_id,
                deposit.token_address.lower() if deposit.token_address is not None else None,
            )
            for deposit in watched
        }
        asset_symbol_by_token = {
            asset.token_address.lower(): asset.symbol for asset in self.chain.assets if asset.token_address is not None
        }
        token_addresses = sorted(asset_symbol_by_token)
        deposit_addresses = sorted(invoice_and_own_token_by_deposit) if token_addresses else []
        # Back from the cursor, as for DepositSwept above.
        from_block = (
            max(
                last_block_number - self.reread_blocks,
                scan_cursor_block_number - (LOG_RANGES_PER_TICK - 1) * self.log_range_blocks,
                -1,
            )
            + 1
        )
        ranges_read = 0
        while ranges_read < LOG_RANGES_PER_TICK and from_block <= target_block_number:
            to_block = min(target_block_number, from_block + self.log_range_blocks - 1)
            # Every chunk's logs are read before anything is written, so a refused chunk rereads the whole range.
            try:
                log_entries_per_chunk = await self.read_transfer_logs_per_deposit_chunk(
                    endpoint,
                    token_addresses=token_addresses,
                    deposit_addresses=deposit_addresses,
                    from_block=from_block,
                    to_block=to_block,
                )
            except EvmLogRangeRefusedError as refusal:
                self.narrow_log_range_after_refusal(
                    refusal, stream_name=CursorStream.TOKEN_TRANSFER_LOGS, from_block=from_block, to_block=to_block
                )
                # Only trim blocks already scanned. Reserve one remaining range for new blocks even when the
                # endpoint narrows to 100; the combined finalized/unfinal reread must not consume every tick.
                from_block = max(
                    from_block,
                    scan_cursor_block_number
                    + 1
                    - max(LOG_RANGES_PER_TICK - ranges_read - 1, 0) * self.log_range_blocks,
                )
                continue
            except EvmLogBeyondHeadRefusedError:
                if to_block <= finalized_block_number:
                    raise
                log.emit(
                    logger,
                    event=TransferLogsStoppedAtFinalized(
                        chain_key=self.chain.key,
                        refused_to_block_number=to_block,
                        finalized_block_number=finalized_block_number,
                    ),
                )
                target_block_number = finalized_block_number
                continue
            self.widen_log_range_after_clean_read()
            transfer_entries = select_watched_token_transfer_entries(
                log_entries_per_chunk,
                invoice_and_own_token_by_deposit=invoice_and_own_token_by_deposit,
                asset_symbol_by_token=asset_symbol_by_token,
            )
            # A log entry has no block time (`blockTimestamp` is optional in the execution-apis Log schema), so the
            # heads of the blocks holding a transfer are read; finality rewrites it if a reorg moves the transfer.
            transfer_block_numbers = sorted({entry.block_number for _, _, entry in transfer_entries})
            timestamp_by_block_number = {
                head.number: head.timestamp
                for head in (
                    await endpoint.read_block_heads_by_number(transfer_block_numbers) if transfer_block_numbers else []
                )
            }
            payments, stray_payments = split_token_transfers_into_payments_and_stray_payments(
                transfer_entries,
                timestamp_by_block_number=timestamp_by_block_number,
                invoice_and_own_token_by_deposit=invoice_and_own_token_by_deposit,
                asset_symbol_by_token=asset_symbol_by_token,
            )
            senders_with_code = await self.find_senders_with_code(
                endpoint, senders=[payment.sender for payment in payments + stray_payments if payment.sender]
            )
            async with self.session_factory() as session, session.begin():
                readings = self.readings_on(session)
                await readings.record_payments(
                    payments, senders_with_code=senders_with_code, finalized_block_number=finalized_block_number
                )
                await readings.record_stray_payments(
                    stray_payments, senders_with_code=senders_with_code, finalized_block_number=finalized_block_number
                )
                await ChainCursorRepository(session=session).write_cursor(
                    self.chain.key,
                    stream_name=CursorStream.TOKEN_TRANSFER_LOGS,
                    last_block_number=max(scan_cursor_block_number, to_block),
                )
            from_block = to_block + 1
            ranges_read += 1
        return from_block > target_block_number

    async def read_transfer_logs_per_deposit_chunk(
        self,
        endpoint: EvmRpcEndpoint,
        *,
        token_addresses: list[str],
        deposit_addresses: list[str],
        from_block: int,
        to_block: int,
    ) -> list[list[LogEntry]]:
        """One range's Transfer logs, one filter per ``DEPOSITS_PER_LOG_FILTER`` deposits in chunk order, at most
        ``CONCURRENT_TRANSFER_LOG_READS`` in flight. All or nothing: the first failure cancels the other reads and is
        raised as itself, a range refusal first when one is among them, then a refusal past the head (another read's
        failure, a 429 say, must not hide one and fail the tick over instead of narrowing or stopping; 2026-10-13)."""
        concurrent_reads = asyncio.Semaphore(CONCURRENT_TRANSFER_LOG_READS)

        async def read_deposit_chunk(deposit_chunk: tuple[str, ...]) -> list[LogEntry]:
            async with concurrent_reads:
                return await endpoint.read_logs(
                    addresses=token_addresses,
                    topics=[TRANSFER_TOPIC, None, [address_topic(address) for address in deposit_chunk]],
                    from_block=from_block,
                    to_block=to_block,
                )

        try:
            async with asyncio.TaskGroup() as task_group:
                chunk_reads = [
                    task_group.create_task(read_deposit_chunk(deposit_chunk))
                    for deposit_chunk in batched(deposit_addresses, DEPOSITS_PER_LOG_FILTER, strict=False)
                ]
        except ExceptionGroup as failed_reads:
            # The group holds the read that failed first, then any that failed while being cancelled.
            range_refusals = [
                failure for failure in failed_reads.exceptions if isinstance(failure, EvmLogRangeRefusedError)
            ]
            beyond_head_refusals = [
                failure for failure in failed_reads.exceptions if isinstance(failure, EvmLogBeyondHeadRefusedError)
            ]
            raise (range_refusals or beyond_head_refusals or failed_reads.exceptions)[0] from None
        return [chunk_read.result() for chunk_read in chunk_reads]

    def narrow_log_range_after_refusal(
        self, refusal: EvmLogRangeRefusedError, *, stream_name: CursorStream, from_block: int, to_block: int
    ) -> None:
        """Halve the refused range, not below ``MINIMUM_LOG_RANGE_BLOCKS``, while ticks stay on this endpoint and
        until enough clean reads widen it again (``widen_log_range_after_clean_read``); a refused range at or under it
        re-raises, failing the tick so the next one fails over (2026-10-12)."""
        refused_range_blocks = to_block - from_block + 1
        if refused_range_blocks <= MINIMUM_LOG_RANGE_BLOCKS:
            raise refusal
        self.log_range_blocks = max(refused_range_blocks // 2, MINIMUM_LOG_RANGE_BLOCKS)
        self.clean_log_reads_since_narrowed = 0
        log.emit(
            logger,
            event=LogRangeNarrowed(
                chain_key=self.chain.key,
                stream_name=stream_name.value,
                from_block_number=from_block,
                refused_range_blocks=refused_range_blocks,
                next_range_blocks=self.log_range_blocks,
            ),
        )

    def widen_log_range_after_clean_read(self) -> None:
        """Count a passed log read; after ``LOG_RANGE_WIDEN_AFTER_CLEAN_READS`` in a row a narrowed range doubles, up
        to ``LOG_RANGE_BLOCKS`` (a refusal halves it again)."""
        if self.log_range_blocks >= LOG_RANGE_BLOCKS:
            return
        self.clean_log_reads_since_narrowed += 1
        if self.clean_log_reads_since_narrowed >= LOG_RANGE_WIDEN_AFTER_CLEAN_READS:
            self.log_range_blocks = min(self.log_range_blocks * 2, LOG_RANGE_BLOCKS)
            self.clean_log_reads_since_narrowed = 0

    async def find_senders_with_code(self, endpoint: EvmRpcEndpoint, *, senders: list[str]) -> set[str]:
        unique_senders = sorted(set(senders))
        return await endpoint.read_addresses_with_code(unique_senders) if unique_senders else set()

    async def read_balances(
        self, endpoint: EvmRpcEndpoint, *, watched: list[WatchedDeposit], block_heads: BlockHeads
    ) -> list[DepositBalances]:
        """Each deposit's own asset at the finalized block and at ``latest``; a token invoice's deposit also its native
        balance at ``latest`` (stray native money, INC-001). At most two calls a deposit at ``latest`` whatever the
        asset count: stray tokens are seen in the Transfer logs, which already name every token."""
        own_asset_calls: list[Multicall3Call] = [
            encode_native_balance_call(deposit.deposit_address)
            if deposit.token_address is None
            else encode_token_balance_call(deposit.token_address, holder_address=deposit.deposit_address)
            for deposit in watched
        ]
        if not own_asset_calls:
            return []
        token_deposits = [deposit for deposit in watched if deposit.token_address is not None]
        native_calls = [encode_native_balance_call(deposit.deposit_address) for deposit in token_deposits]
        finalized_results = await endpoint.aggregate3(own_asset_calls, block_hash=block_heads.finalized.hash)
        latest_all_results = await endpoint.aggregate3(
            own_asset_calls + native_calls, block_hash=block_heads.latest.hash
        )
        latest_results = latest_all_results[: len(own_asset_calls)]
        latest_native_balance_by_invoice = {
            deposit.invoice_id: decode_uint256(result.return_data)
            for deposit, result in zip(token_deposits, latest_all_results[len(own_asset_calls) :], strict=True)
            if result.succeeded
        }
        balances: list[DepositBalances] = []
        for deposit, finalized_result, latest_result in zip(watched, finalized_results, latest_results, strict=True):
            if not (finalized_result.succeeded and latest_result.succeeded):
                continue
            if deposit.token_address is not None and deposit.invoice_id not in latest_native_balance_by_invoice:
                # Applying the reading without its native balance would move the block the next native scan starts
                # from past a rise it never matched, leaving that money with no sender (2026-10-12).
                continue
            balances.append(
                DepositBalances(
                    invoice_id=deposit.invoice_id,
                    finalized_balance_base_units=decode_uint256(finalized_result.return_data),
                    latest_balance_base_units=decode_uint256(latest_result.return_data),
                    latest_native_balance_base_units=latest_native_balance_by_invoice.get(deposit.invoice_id),
                )
            )
        return balances

    async def settle_payment_finality(
        self, endpoint: EvmRpcEndpoint, *, block_heads: BlockHeads, unfinal: list[UnfinalPayment]
    ) -> None:
        """Payments and stray payments whose block is now final, settled per transaction, invoice and asset: rows
        still at their canonical block go final as they are; otherwise the transaction's receipt is the truth. A reorg
        can re-mine a token transfer at another log index (the index is the log's position in its block), and the
        re-read of the unfinal range may already have inserted it there, so the rows are replaced by the receipt's
        matching Transfer logs (a native payment: the transaction itself), or dropped when it is gone or reverted
        (2026-10-09). ``unfinal`` is the tick's own read of the payment rows at or below the finalized block."""
        finalized_number = block_heads.finalized.number
        async with self.session_factory() as session:
            unfinal_stray = await self.readings_on(session).list_unfinal_stray_payments(
                self.chain.key, up_to_block_number=finalized_number
            )
        if not unfinal and not unfinal_stray:
            return
        block_numbers = sorted({row.block_number for row in unfinal + unfinal_stray})
        canonical_heads = {head.number: head for head in await endpoint.read_block_heads_by_number(block_numbers)}
        payment_finality = await self.resolve_finality(
            unfinal, endpoint=endpoint, canonical_heads=canonical_heads, finalized_number=finalized_number
        )
        stray_finality = await self.resolve_finality(
            unfinal_stray, endpoint=endpoint, canonical_heads=canonical_heads, finalized_number=finalized_number
        )
        senders_with_code = await self.find_senders_with_code(
            endpoint,
            senders=[
                payment.sender
                for finality in (payment_finality, stray_finality)
                for transaction in finality.canonical_transactions
                for payment in transaction.payments
            ],
        )
        async with self.session_factory() as session, session.begin():
            readings = self.readings_on(session)
            # A payment row with no transaction whose block left the chain stays as it is (none is written today).
            await readings.settle_payment_finality(
                confirmed=payment_finality.confirmed,
                canonical_transactions=payment_finality.canonical_transactions,
                senders_with_code=senders_with_code,
            )
            await readings.settle_stray_payment_finality(
                confirmed=stray_finality.confirmed,
                canonical_transactions=stray_finality.canonical_transactions,
                senders_with_code=senders_with_code,
                dropped_stray_payment_ids=stray_finality.rows_off_chain,
            )

    async def resolve_finality(
        self,
        unfinal: list[UnfinalPayment],
        *,
        endpoint: EvmRpcEndpoint,
        canonical_heads: dict[int, BlockHead],
        finalized_number: int,
    ) -> ResolvedFinality:
        rows_by_transaction: dict[tuple[str, str, str], list[UnfinalPayment]] = {}
        confirmed: dict[int, FinalPaymentBlock] = {}
        rows_off_chain: list[int] = []

        def is_at_canonical_block(payment: UnfinalPayment) -> bool:
            head = canonical_heads.get(payment.block_number)
            return head is not None and head.hash == payment.block_hash

        def confirm_in_place(payment: UnfinalPayment) -> None:
            head = canonical_heads[payment.block_number]
            confirmed[payment.payment_id] = FinalPaymentBlock(
                block_number=head.number, block_hash=head.hash, block_timestamp=head.timestamp
            )

        for payment in unfinal:
            if payment.transaction_hash is None:
                # No transaction to look up: final while its block is canonical, gone once it is not.
                if is_at_canonical_block(payment):
                    confirm_in_place(payment)
                else:
                    rows_off_chain.append(payment.payment_id)
                continue
            key = (payment.invoice_id, payment.asset_symbol, payment.transaction_hash)
            rows_by_transaction.setdefault(key, []).append(payment)
        moved_transactions: list[tuple[str, str, str]] = []
        for key, rows in rows_by_transaction.items():
            if all(is_at_canonical_block(row) for row in rows):
                for row in rows:
                    confirm_in_place(row)
            else:
                moved_transactions.append(key)
        receipts = (
            await endpoint.read_transaction_receipts(
                [transaction_hash for _, _, transaction_hash in moved_transactions]
            )
            if moved_transactions
            else []
        )
        settled_receipts: list[tuple[tuple[str, str, str], TransactionReceipt | None]] = [
            (key, receipt)
            for key, receipt in zip(moved_transactions, receipts, strict=True)
            # Re-mined but not final yet: settled on a later tick.
            if receipt is None or receipt.block_number <= finalized_number
        ]
        # The canonical header's timestamp of each block a moved transaction now sits in (the credit's time).
        moved_block_numbers = sorted(
            {receipt.block_number for _, receipt in settled_receipts if receipt is not None and receipt.succeeded}
        )
        moved_timestamps = {
            head.number: head.timestamp
            for head in (await endpoint.read_block_heads_by_number(moved_block_numbers) if moved_block_numbers else [])
        }
        return ResolvedFinality(
            confirmed=confirmed,
            canonical_transactions=[
                CanonicalTransactionPayments(
                    invoice_id=invoice_id,
                    asset_symbol=asset_symbol,
                    transaction_hash=transaction_hash,
                    payments=find_canonical_payments(
                        rows_by_transaction[invoice_id, asset_symbol, transaction_hash],
                        receipt=receipt,
                        block_timestamp_by_number=moved_timestamps,
                    ),
                )
                for (invoice_id, asset_symbol, transaction_hash), receipt in settled_receipts
            ],
            rows_off_chain=rows_off_chain,
        )
