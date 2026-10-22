"""StaticDepositScanService: the watcher's scan of static deposit addresses (PLAN.md section 17).

Static addresses are watched for good, so they are not read every tick like an open invoice and not named in the
invoices' Transfer filters (both would grow every tick's reads with the address count). They are split in two tiers,
like the invoices' rescan (rescan_service.py):

- hot: made or paid within the chain's ``static_deposit_hot_window_seconds`` (a day), a pass every
  ``static_deposit_hot_scan_interval_seconds`` (a minute);
- cold: every other address, a pass every ``static_deposit_cold_scan_interval_seconds`` (20 minutes).

A pass fixes its tier's cutoff when it starts and reads one slice a tick in (created_at, id) order: each address in
every configured coin, one Multicall3 ``aggregate3`` pinned to the finalized block hash, paced to end in about half
its interval. Both tiers share ``STATIC_DEPOSIT_MAXIMUM_BALANCE_CALLS_PER_TICK`` (no tick
does unbounded work): the hot tier goes first, but while a cold pass is running or due it gets at most
``STATIC_DEPOSIT_HOT_TIER_SHARE_OF_BUDGET`` of it, so the cold tier always moves; the cold tier reads what is left. More
addresses lengthen a pass, never a tick. A pass slower than its interval is logged, and the tier stays a
``static_deposit_scan_slow`` operator alert (``find_slow_pass_findings``, read by the watcher's minute alert check)
until one of its passes ends in time. Passes live in memory: a restart starts both afresh.

Only a slice with new money (``StaticDepositReadingsService.find_rises``) reads Transfer logs, every token that rose,
from the oldest such address's last scanned block (at most two cold intervals or 15 minutes back, whichever is longer)
to the finalized block: per ``ADDRESSES_PER_LOG_FILTER`` of those addresses, one ``eth_getLogs`` per
``STATIC_DEPOSIT_ATTRIBUTION_MAXIMUM_BLOCKS`` of the window (Polygon's 1,200-block window: one; Arbitrum's 9,600: five).
Once the endpoint refuses a range, the rest of that read goes ``MINIMUM_LOG_RANGE_BLOCKS`` at a time (Polygon: 12 a
filter; Arbitrum would need 96, past the cap, so it records that slice's money with no sender), and a read never makes
more than ``STATIC_DEPOSIT_MAXIMUM_LOG_CALLS_PER_SLICE`` calls, so a tick's two slices make at most twice that. The logs only attribute (a sender and a transaction for each row); the
money is the balances' to record, so a read that fails or runs out of calls records it with no sender and the tick
goes on. The slice is applied in one transaction. A failed balance read fails the tick (the slice is read again next
tick). A gross below what an address's rows hold, and credits waiting for a price, are logged once when an address
enters that state, not every pass.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import batched
import math
import time

from eth_utils import to_checksum_address
from pydantic import JsonValue
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import (
    MULTICALL3_CHUNK_SIZE,
    EvmRpcEndpoint,
    decode_uint256,
    encode_native_balance_call,
    encode_token_balance_call,
)
from app.infrastructure.evm_rpc_api.errors import EvmLogRangeRefusedError, EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, LogEntry
from app.payments.chain_watcher.canonical_payments import address_topic
from app.payments.chain_watcher.log_events import (
    StaticDepositAttributionSkipped,
    StaticDepositScanSlowerThanInterval,
    StaticDepositsScanned,
)
from app.payments.chain_watcher.types import ReconciliationFinding, StaticDepositScanPass
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.settlements.service import TRANSFER_TOPIC
from app.payments.static_deposit_addresses.log_events import StaticDepositCreditDeferred, StaticDepositGrossWentDown
from app.payments.static_deposit_addresses.readings_service import StaticDepositReadingsService
from app.payments.static_deposit_addresses.types import (
    ScannedStaticDeposit,
    StaticDepositReading,
    StaticDepositRise,
    StaticDepositTransfer,
)
from app.shared import logging as log

logger = log.get_logger(__name__)

# Two aggregate3 eth_calls of 500 a tick, both tiers together: 100 addresses with Polygon's 10 coins.
STATIC_DEPOSIT_MAXIMUM_BALANCE_CALLS_PER_TICK = 2 * MULTICALL3_CHUNK_SIZE
# While a cold pass runs or is due, the hot tier reads at most this share of the budget: thousands of hot addresses
# must not stall the cold tier's pass for hours.
STATIC_DEPOSIT_HOT_TIER_SHARE_OF_BUDGET = 0.75
STATIC_DEPOSIT_SCAN_SHARE_OF_INTERVAL = 0.5
# How far back the Transfer logs of new money are read at most: two cold scan intervals, since a cold address's money
# can be that old when its pass finds it, and never less than the floor, which covers a hot pass. Older money is
# recorded with no sender.
STATIC_DEPOSIT_ATTRIBUTION_COLD_INTERVALS = 2
STATIC_DEPOSIT_ATTRIBUTION_MINIMUM_SECONDS = 900
# The widest single eth_getLogs, and the block count once an endpoint refused one (as the tick's own log reads,
# service.py).
STATIC_DEPOSIT_ATTRIBUTION_MAXIMUM_BLOCKS = 2_000
MINIMUM_LOG_RANGE_BLOCKS = 100
ADDRESSES_PER_LOG_FILTER = 100
# The most eth_getLogs one slice's attribution makes; past it the slice's new money is recorded with no sender.
STATIC_DEPOSIT_MAXIMUM_LOG_CALLS_PER_SLICE = 50


class StaticDepositLogCallsSpentError(Exception):
    """A slice's attribution needs more than ``STATIC_DEPOSIT_MAXIMUM_LOG_CALLS_PER_SLICE`` eth_getLogs."""


type SessionFactory = Callable[[], AsyncSession]


def name_scan_tier(*, is_hot_tier: bool) -> str:
    return "hot" if is_hot_tier else "cold"


class StaticDepositScanService:
    def __init__(
        self, *, chain: EvmChainConfiguration, session_factory: SessionFactory, redis: Redis, wiring: InvoiceWiring
    ) -> None:
        self.chain = chain
        self.session_factory = session_factory
        self.redis = redis
        self.wiring = wiring
        # Per tier (True: hot): its running pass, and when its last one started (Unix seconds).
        self.running_pass_by_tier: dict[bool, StaticDepositScanPass | None] = {True: None, False: None}
        self.last_pass_started_at_timestamp_by_tier: dict[bool, float | None] = {True: None, False: None}
        # (address id, coin) pairs now in a logged state, so each is logged when it enters it, not every pass.
        self.gross_below_recorded_keys: set[tuple[str, str]] = set()
        self.credit_deferred_keys: set[tuple[str, str]] = set()
        # Per tier: (pass seconds, interval seconds) of its last pass when that ran over its interval; cleared by one
        # in time.
        self.slow_pass_by_tier: dict[bool, tuple[float, float]] = {}

    def readings_on(self, session: AsyncSession) -> StaticDepositReadingsService:
        return StaticDepositReadingsService.on_session(session, redis=self.redis, wiring=self.wiring)

    def addresses_per_aggregate3_chunk(self) -> int:
        return max(1, MULTICALL3_CHUNK_SIZE // len(self.chain.assets))

    def tier_interval_seconds(self, *, is_hot_tier: bool) -> float:
        if is_hot_tier:
            return self.chain.static_deposit_hot_scan_interval_seconds
        return self.chain.static_deposit_cold_scan_interval_seconds

    def is_pass_running_or_due(self, *, is_hot_tier: bool, now: float) -> bool:
        if self.running_pass_by_tier[is_hot_tier] is not None:
            return True
        last_started = self.last_pass_started_at_timestamp_by_tier[is_hot_tier]
        return last_started is None or not 0 <= now - last_started < self.tier_interval_seconds(is_hot_tier=is_hot_tier)

    async def scan_when_due(self, endpoint: EvmRpcEndpoint, *, block_heads: BlockHeads) -> None:
        """One slice of each tier's running pass within the tick's balance call budget: the hot tier first, held to
        its share while the cold tier has a pass to run, then the cold tier with the rest."""
        budget_addresses = max(1, STATIC_DEPOSIT_MAXIMUM_BALANCE_CALLS_PER_TICK // len(self.chain.assets))
        hot_tier_addresses = (
            max(1, math.floor(budget_addresses * STATIC_DEPOSIT_HOT_TIER_SHARE_OF_BUDGET))
            if self.is_pass_running_or_due(is_hot_tier=False, now=time.time())
            else budget_addresses
        )
        hot_addresses_read = await self.scan_tier_slice(
            endpoint, block_heads=block_heads, is_hot_tier=True, maximum_addresses=hot_tier_addresses
        )
        cold_tier_addresses = budget_addresses - hot_addresses_read
        if cold_tier_addresses > 0:
            await self.scan_tier_slice(
                endpoint, block_heads=block_heads, is_hot_tier=False, maximum_addresses=cold_tier_addresses
            )

    def find_slow_pass_findings(self) -> list[ReconciliationFinding]:
        """A ``static_deposit_scan_slow`` alert per tier whose last pass ran over its interval."""
        return [
            ReconciliationFinding(
                problem_key=f"static_deposit_scan_slow:{self.chain.key}:{name_scan_tier(is_hot_tier=is_hot_tier)}",
                alert_kind="static_deposit_scan_slow",
                invoice_id=None,
                message=f"The {name_scan_tier(is_hot_tier=is_hot_tier)} static deposit address scan on "
                f"{self.chain.name} took {pass_seconds:.0f} s for a {interval_seconds:.0f} s interval: raise "
                "STATIC_DEPOSIT_MAXIMUM_BALANCE_CALLS_PER_TICK or cap PAY_MAXIMUM_STATIC_DEPOSIT_ADDRESSES_PER_ACCOUNT "
                "lower (watcher log `chain_watcher_static_deposit_scan`).",
                columns={
                    "scan tier": name_scan_tier(is_hot_tier=is_hot_tier),
                    "pass seconds": f"{pass_seconds:.0f}",
                    "interval seconds": f"{interval_seconds:.0f}",
                },
            )
            for is_hot_tier, (pass_seconds, interval_seconds) in sorted(self.slow_pass_by_tier.items())
        ]

    async def scan_tier_slice(
        self, endpoint: EvmRpcEndpoint, *, block_heads: BlockHeads, is_hot_tier: bool, maximum_addresses: int
    ) -> int:
        """One slice of the tier's pass when one is due or running; how many addresses it read."""
        now = time.time()
        scan_pass = await self.find_or_start_pass(is_hot_tier=is_hot_tier, now=now)
        if scan_pass is None:
            return 0
        limit = min(scan_pass.addresses_per_tick, maximum_addresses)
        async with self.session_factory() as session:
            page = await self.readings_on(session).list_scan_page(
                self.chain.key,
                hot=is_hot_tier,
                hot_since=scan_pass.hot_since,
                after=scan_pass.last_scanned,
                limit=limit,
            )
        if page:
            scan_pass.addresses_with_new_money += await self.scan_slice(endpoint, page=page, block_heads=block_heads)
            scan_pass.last_scanned = page[-1]
            scan_pass.addresses_read += len(page)
        scan_pass.ticks_taken += 1
        if len(page) < limit:
            self.end_pass(scan_pass, now=now)
        return len(page)

    async def find_or_start_pass(self, *, is_hot_tier: bool, now: float) -> StaticDepositScanPass | None:
        running_pass = self.running_pass_by_tier[is_hot_tier]
        if running_pass is not None:
            return running_pass
        interval_seconds = self.tier_interval_seconds(is_hot_tier=is_hot_tier)
        last_started = self.last_pass_started_at_timestamp_by_tier[is_hot_tier]
        if last_started is not None and 0 <= now - last_started < interval_seconds:
            return None
        hot_since = datetime.now(UTC) - timedelta(seconds=self.chain.static_deposit_hot_window_seconds)
        async with self.session_factory() as session:
            address_count = await self.readings_on(session).count_chain_static_deposit_addresses(
                self.chain.key, hot=is_hot_tier, hot_since=hot_since
            )
        ticks_per_pass = max(
            1, math.floor(interval_seconds * STATIC_DEPOSIT_SCAN_SHARE_OF_INTERVAL / self.chain.poll_interval_seconds)
        )
        started_pass = StaticDepositScanPass(
            is_hot_tier=is_hot_tier,
            hot_since=hot_since,
            started_at_timestamp=now,
            addresses_per_tick=max(self.addresses_per_aggregate3_chunk(), math.ceil(address_count / ticks_per_pass)),
        )
        self.running_pass_by_tier[is_hot_tier] = started_pass
        self.last_pass_started_at_timestamp_by_tier[is_hot_tier] = now
        return started_pass

    def end_pass(self, scan_pass: StaticDepositScanPass, *, now: float) -> None:
        self.running_pass_by_tier[scan_pass.is_hot_tier] = None
        if scan_pass.addresses_read == 0:
            return
        scan_tier = name_scan_tier(is_hot_tier=scan_pass.is_hot_tier)
        interval_seconds = self.tier_interval_seconds(is_hot_tier=scan_pass.is_hot_tier)
        pass_seconds = now - scan_pass.started_at_timestamp
        log.emit(
            logger,
            event=StaticDepositsScanned(
                chain_key=self.chain.key,
                scan_tier=scan_tier,
                address_count=scan_pass.addresses_read,
                addresses_with_new_money=scan_pass.addresses_with_new_money,
                tick_count=scan_pass.ticks_taken,
                pass_seconds=round(pass_seconds, 1),
            ),
        )
        if pass_seconds <= interval_seconds:
            self.slow_pass_by_tier.pop(scan_pass.is_hot_tier, None)
        else:
            self.slow_pass_by_tier[scan_pass.is_hot_tier] = (pass_seconds, interval_seconds)
            log.emit(
                logger,
                event=StaticDepositScanSlowerThanInterval(
                    chain_key=self.chain.key,
                    scan_tier=scan_tier,
                    pass_seconds=round(pass_seconds, 1),
                    interval_seconds=interval_seconds,
                ),
            )

    async def scan_slice(
        self, endpoint: EvmRpcEndpoint, *, page: list[ScannedStaticDeposit], block_heads: BlockHeads
    ) -> int:
        """Read, attribute and apply one slice; how many of its addresses had new money."""
        readings = await self.read_balances(endpoint, page=page, block_heads=block_heads)
        slice_address_ids = {scanned.static_deposit_address_id for scanned in page}
        async with self.session_factory() as session:
            rise_search = await self.readings_on(session).find_rises(
                self.chain.key, readings=readings, finalized_block_number=block_heads.finalized.number
            )
        self.log_entered_states(
            slice_address_ids,
            current_keys=rise_search.gross_below_recorded_keys,
            logged_keys=self.gross_below_recorded_keys,
            build_event=lambda key: StaticDepositGrossWentDown(static_deposit_address_id=key[0], asset_symbol=key[1]),
        )
        rises = rise_search.rises
        if not rises:
            # Nothing to record; money still held is asked to be swept again (a frozen key's sweep ended, say).
            if any(self.holds_sweepable_money(reading) for reading in readings):
                await self.apply_slice_readings(readings=readings, transfers=[], block_heads=block_heads)
            return 0
        transfers = await self.read_transfers(
            endpoint, rises=rises, finalized_block_number=block_heads.finalized.number
        )
        credit_deferred_keys = await self.apply_slice_readings(
            readings=readings, transfers=transfers, block_heads=block_heads
        )
        self.log_entered_states(
            slice_address_ids,
            current_keys=credit_deferred_keys,
            logged_keys=self.credit_deferred_keys,
            build_event=lambda key: StaticDepositCreditDeferred(static_deposit_address_id=key[0], asset_symbol=key[1]),
        )
        return len({rise.static_deposit_address_id for rise in rises})

    def log_entered_states(
        self,
        slice_address_ids: set[str],
        *,
        current_keys: frozenset[tuple[str, str]],
        logged_keys: set[tuple[str, str]],
        build_event: Callable[[tuple[str, str]], StaticDepositGrossWentDown | StaticDepositCreditDeferred],
    ) -> None:
        """Logs each (address id, coin) pair of the slice that entered the state since the last pass, and forgets
        the slice's pairs that left it."""
        for key in sorted(current_keys - logged_keys):
            log.emit(logger, event=build_event(key))
        logged_keys.difference_update({key for key in logged_keys if key[0] in slice_address_ids})
        logged_keys.update(current_keys)

    def holds_sweepable_money(self, reading: StaticDepositReading) -> bool:
        return any(
            reading.balances_by_symbol.get(asset.symbol, 0) >= asset.minimum_sweep_base_units
            for asset in self.chain.assets
        )

    async def apply_slice_readings(
        self, *, readings: list[StaticDepositReading], transfers: list[StaticDepositTransfer], block_heads: BlockHeads
    ) -> frozenset[tuple[str, str]]:
        async with self.session_factory() as session, session.begin():
            return await self.readings_on(session).apply_readings(
                self.chain.key,
                readings=readings,
                transfers=transfers,
                finalized_block_number=block_heads.finalized.number,
            )

    async def read_balances(
        self, endpoint: EvmRpcEndpoint, *, page: list[ScannedStaticDeposit], block_heads: BlockHeads
    ) -> list[StaticDepositReading]:
        calls = [
            encode_native_balance_call(scanned.deposit_address)
            if asset.token_address is None
            else encode_token_balance_call(asset.token_address, holder_address=scanned.deposit_address)
            for scanned in page
            for asset in self.chain.assets
        ]
        results = await endpoint.aggregate3(calls, block_hash=block_heads.finalized.hash)
        asset_count = len(self.chain.assets)
        return [
            StaticDepositReading(
                static_deposit_address_id=scanned.static_deposit_address_id,
                # A failed call is left out: that coin waits for the next pass.
                balances_by_symbol={
                    asset.symbol: decode_uint256(result.return_data)
                    for asset, result in zip(
                        self.chain.assets, results[index * asset_count : (index + 1) * asset_count], strict=True
                    )
                    if result.succeeded
                },
            )
            for index, scanned in enumerate(page)
        ]

    async def read_transfers(
        self, endpoint: EvmRpcEndpoint, *, rises: list[StaticDepositRise], finalized_block_number: int
    ) -> list[StaticDepositTransfer]:
        """The Transfer logs into the addresses with new token money, best effort (module docstring)."""
        token_rises = [rise for rise in rises if rise.token_address is not None]
        if not token_rises:
            return []
        maximum_blocks = self.chain.blocks_in_seconds(
            max(
                STATIC_DEPOSIT_ATTRIBUTION_COLD_INTERVALS * self.chain.static_deposit_cold_scan_interval_seconds,
                STATIC_DEPOSIT_ATTRIBUTION_MINIMUM_SECONDS,
            )
        )
        earliest_block = max(finalized_block_number - maximum_blocks + 1, 0)
        from_block = max(
            min(
                rise.last_scanned_block_number + 1 if rise.last_scanned_block_number is not None else earliest_block
                for rise in token_rises
            ),
            earliest_block,
        )
        token_addresses = sorted({rise.token_address for rise in token_rises if rise.token_address is not None})
        deposit_addresses = sorted({rise.deposit_address.lower() for rise in token_rises})
        try:
            log_entries = await self.read_transfer_logs(
                endpoint,
                token_addresses=token_addresses,
                deposit_addresses=deposit_addresses,
                from_block=from_block,
                to_block=finalized_block_number,
            )
        except (EvmRpcUnavailableError, StaticDepositLogCallsSpentError) as failure:
            log.emit(
                logger,
                event=StaticDepositAttributionSkipped(
                    chain_key=self.chain.key,
                    from_block_number=from_block,
                    to_block_number=finalized_block_number,
                    error_type=type(failure).__name__,
                ),
            )
            return []
        transfers: list[StaticDepositTransfer] = []
        # An endpoint may repeat a log across ranges or answers; each (transaction, log index) is one transfer.
        seen_log_keys: set[tuple[str, int]] = set()
        for entry in log_entries:
            amount = int(entry.data, 16) if entry.data not in {"", "0x"} else 0
            log_key = (entry.transaction_hash.lower(), entry.log_index)
            if amount == 0 or len(entry.topics) < 3 or log_key in seen_log_keys:
                continue
            seen_log_keys.add(log_key)
            transfers.append(
                StaticDepositTransfer(
                    deposit_address=to_checksum_address("0x" + entry.topics[2][-40:]),
                    token_address=to_checksum_address(entry.address),
                    transaction_hash=entry.transaction_hash,
                    log_index=entry.log_index,
                    sender=to_checksum_address("0x" + entry.topics[1][-40:]),
                    amount_base_units=amount,
                    block_number=entry.block_number,
                )
            )
        return transfers

    async def read_transfer_logs(
        self,
        endpoint: EvmRpcEndpoint,
        *,
        token_addresses: list[str],
        deposit_addresses: list[str],
        from_block: int,
        to_block: int,
    ) -> list[LogEntry]:
        """Each filter of addresses over the window in ranges of ``STATIC_DEPOSIT_ATTRIBUTION_MAXIMUM_BLOCKS``, or of
        ``MINIMUM_LOG_RANGE_BLOCKS`` for the rest of the read once the endpoint refused one; a refusal at the narrow
        size, or a read past ``STATIC_DEPOSIT_MAXIMUM_LOG_CALLS_PER_SLICE`` calls, raises."""
        log_entries: list[LogEntry] = []
        range_blocks = STATIC_DEPOSIT_ATTRIBUTION_MAXIMUM_BLOCKS
        calls_made = 0
        for deposit_chunk in batched(deposit_addresses, ADDRESSES_PER_LOG_FILTER, strict=False):
            topics: list[JsonValue] = [TRANSFER_TOPIC, None, [address_topic(address) for address in deposit_chunk]]
            range_from_block = from_block
            while range_from_block <= to_block:
                if calls_made >= STATIC_DEPOSIT_MAXIMUM_LOG_CALLS_PER_SLICE:
                    raise StaticDepositLogCallsSpentError(f"more than {calls_made} eth_getLogs calls")
                calls_made += 1
                range_to_block = min(range_from_block + range_blocks - 1, to_block)
                try:
                    log_entries.extend(
                        await endpoint.read_logs(
                            addresses=token_addresses,
                            topics=topics,
                            from_block=range_from_block,
                            to_block=range_to_block,
                        )
                    )
                except EvmLogRangeRefusedError:
                    if range_blocks <= MINIMUM_LOG_RANGE_BLOCKS:
                        raise
                    range_blocks = MINIMUM_LOG_RANGE_BLOCKS
                    continue
                range_from_block = range_to_block + 1
        return log_entries
