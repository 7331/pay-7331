"""DepositRescanService: the watcher's rescan (PLAN.md section 4, late and stray money; section 5).

Every deposit ever made is read in every configured asset at the finalized block and handed to
``ChainReadingsService.apply_rescan``; that is what finds late money and anything the log reads missed. Deposits are
split in two tiers (2026-10-13), since one pass over all of them grew past its interval at about 45,000 deposits:

- recent: invoices made within ``recent_deposit_window_seconds`` or not closed yet, a pass every
  ``rescan_interval_seconds``;
- old: every other deposit, a pass every ``old_deposit_rescan_interval_seconds``.

A pass fixes its window's cutoff when it starts and pages its creation ranges by (created_at, id), one bounded slice a
tick, paced to end in about half its own interval. Both tiers share the tick's budget of balance calls: the recent
tier goes first, the old tier reads what it left. A failed slice fails the tick and the next tick reads it again.

The recent tier's pass lives in memory: a restart starts it afresh, ten minutes of reads. The old tier's pass and the
start of its last pass are copied to Redis after every applied slice (``OldRescanTierStateStore``, a TTL of twice
its interval), so the several deploys a day resume it instead of rereading the oldest deposits first and starving
the newest old ones (2026-10-13). Losing the copy only means the next pass starts from the first deposit.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import math
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import (
    MULTICALL3_CHUNK_SIZE,
    EvmRpcEndpoint,
    decode_uint256,
    encode_native_balance_call,
    encode_token_balance_call,
)
from app.infrastructure.evm_rpc_api.schemas import BlockHeads
from app.payments.chain_watcher.enums import RescanTier
from app.payments.chain_watcher.log_events import (
    DepositsRescanned,
    OldRescanTierStateUnavailable,
    RescanSlowerThanInterval,
)
from app.payments.chain_watcher.store import OldRescanTierStateStore
from app.payments.chain_watcher.types import OldRescanTierState, RescanPass, RescanPassPosition
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoices.chain_readings_service import ChainReadingsService
from app.payments.invoices.types import (
    DepositCreationPosition,
    DepositCreationRange,
    DepositInCreationOrder,
    RescannedDeposit,
)
from app.payments.invoices.wiring import InvoiceWiring
from app.shared import logging as log

logger = log.get_logger(__name__)

# A tier spreads one pass over this share of its interval worth of ticks: a tick takes longer than
# `poll_interval_seconds`, so pacing for the whole interval would overrun it (2026-10-09).
RESCAN_PASS_SHARE_OF_INTERVAL = 0.5
# At most this many balance calls a tick, both tiers together (6 aggregate3 eth_calls of 500, read one after
# another), so no tick does unbounded work whatever the asset count: every deposit is read in every configured asset,
# so Polygon's 10 assets (2026-10-11) make it 300 deposits a tick. Past about 37,000 recent deposits on Polygon (150
# ticks of 250 while an old pass runs) a recent pass takes longer than half its interval; one longer than the whole
# interval logs `RescanSlowerThanInterval` with its tier.
RESCAN_MAXIMUM_BALANCE_CALLS_PER_TICK = 3_000
# The recent tier has priority, but while an old pass runs it leaves the old tier one aggregate3 eth_call a tick
# (50 deposits with 10 assets), so a busy day that fills the budget with recent deposits slows the old pass instead of
# stopping it. When the recent tier needs less, the old tier takes whatever is left (2026-10-13).
RESCAN_OLD_TIER_RESERVED_BALANCE_CALLS_PER_TICK = MULTICALL3_CHUNK_SIZE
# The old tier's Redis copy outlives one interval with no write (a watcher stopped that long) before it expires.
OLD_RESCAN_TIER_STATE_TTL_SHARE_OF_INTERVAL = 2

type SessionFactory = Callable[[], AsyncSession]


class DepositRescanService:
    def __init__(
        self, *, chain: EvmChainConfiguration, session_factory: SessionFactory, redis: Redis, wiring: InvoiceWiring
    ) -> None:
        self.chain = chain
        self.session_factory = session_factory
        self.redis = redis
        self.wiring = wiring
        self.old_rescan_tier_state_store = OldRescanTierStateStore(client=redis)
        self.rescan_pass_by_tier: dict[RescanTier, RescanPass] = {}
        self.last_pass_started_at_timestamp_by_tier: dict[RescanTier, float] = {}
        # The old tier's Redis copy is read once, on this process's first rescan.
        self.old_rescan_tier_state_restored = False

    def readings_on(self, session: AsyncSession) -> ChainReadingsService:
        return ChainReadingsService.on_session(session, redis=self.redis, wiring=self.wiring)

    def interval_seconds_of(self, tier: RescanTier) -> float:
        if tier is RescanTier.RECENT:
            return self.chain.rescan_interval_seconds
        return self.chain.old_deposit_rescan_interval_seconds

    async def rescan_when_due(self, endpoint: EvmRpcEndpoint, *, block_heads: BlockHeads) -> None:
        """One slice of each tier's running pass, within ``RESCAN_MAXIMUM_BALANCE_CALLS_PER_TICK``. A tier's pass
        starts every interval (or right after a pass that overran it)."""
        if not self.old_rescan_tier_state_restored:
            await self.restore_old_rescan_tier_state()
        now = time.time()
        recent_pass = await self.find_or_start_pass(RescanTier.RECENT, now=now)
        old_pass = await self.find_or_start_pass(RescanTier.OLD, now=now)
        asset_count = len(self.chain.assets)
        balance_calls_left = RESCAN_MAXIMUM_BALANCE_CALLS_PER_TICK
        if recent_pass is not None:
            reserved_balance_calls = RESCAN_OLD_TIER_RESERVED_BALANCE_CALLS_PER_TICK if old_pass is not None else 0
            deposits_read = await self.rescan_slice(
                endpoint,
                rescan_pass=recent_pass,
                block_heads=block_heads,
                now=now,
                limit=min(
                    recent_pass.deposits_per_tick,
                    max(
                        self.deposits_per_aggregate3_chunk(),
                        (balance_calls_left - reserved_balance_calls) // asset_count,
                    ),
                ),
            )
            balance_calls_left -= deposits_read * asset_count
        if old_pass is not None:
            old_limit = min(old_pass.deposits_per_tick, balance_calls_left // asset_count)
            if old_limit > 0:
                await self.rescan_slice(
                    endpoint, rescan_pass=old_pass, block_heads=block_heads, now=now, limit=old_limit
                )

    async def find_or_start_pass(self, tier: RescanTier, *, now: float) -> RescanPass | None:
        """The tier's running pass, a new one when its interval has passed since the last started, else None."""
        running_pass = self.rescan_pass_by_tier.get(tier)
        if running_pass is not None:
            return running_pass
        last_started = self.last_pass_started_at_timestamp_by_tier.get(tier)
        interval_seconds = self.interval_seconds_of(tier)
        # Wall-clock time (the old tier's start is kept in Redis): a clock stepped back starts a pass, not a pause.
        if last_started is not None and 0 <= now - last_started < interval_seconds:
            return None
        creation_ranges = self.creation_ranges_of(tier)
        async with self.session_factory() as session:
            readings = self.readings_on(session)
            deposit_count = sum(
                [
                    await readings.count_deposits_in_creation_range(self.chain.key, creation_range=creation_range)
                    for creation_range in creation_ranges
                ]
            )
        started_pass = RescanPass(
            tier=tier,
            started_at_timestamp=now,
            deposits_per_tick=self.rescan_deposits_per_tick(deposit_count, interval_seconds=interval_seconds),
            creation_ranges=creation_ranges,
        )
        self.rescan_pass_by_tier[tier] = started_pass
        self.last_pass_started_at_timestamp_by_tier[tier] = now
        return started_pass

    async def restore_old_rescan_tier_state(self) -> None:
        """The old tier's running pass and last start from Redis; nothing kept (or Redis down) leaves both unset, so
        a fresh pass starts now."""
        self.old_rescan_tier_state_restored = True
        try:
            state = await self.old_rescan_tier_state_store.find_old_rescan_tier_state(self.chain.key)
        except RedisError:
            log.emit(logger, event=OldRescanTierStateUnavailable(chain_key=self.chain.key, redis_operation="read"))
            return
        if state is None:
            return
        self.last_pass_started_at_timestamp_by_tier[RescanTier.OLD] = state.last_pass_started_at_timestamp
        if state.running_pass is not None:
            self.rescan_pass_by_tier[RescanTier.OLD] = state.running_pass

    async def keep_old_rescan_tier_state(self) -> None:
        """The old tier's running pass (None between passes) and last start, copied to Redis; a failed write is
        logged and the pass goes on in memory."""
        last_started = self.last_pass_started_at_timestamp_by_tier.get(RescanTier.OLD)
        if last_started is None:
            return
        state = OldRescanTierState(
            chain_key=self.chain.key,
            running_pass=self.rescan_pass_by_tier.get(RescanTier.OLD),
            last_pass_started_at_timestamp=last_started,
        )
        try:
            await self.old_rescan_tier_state_store.write_old_rescan_tier_state(
                state,
                ttl_seconds=math.ceil(
                    self.chain.old_deposit_rescan_interval_seconds * OLD_RESCAN_TIER_STATE_TTL_SHARE_OF_INTERVAL
                ),
            )
        except RedisError:
            log.emit(logger, event=OldRescanTierStateUnavailable(chain_key=self.chain.key, redis_operation="write"))

    def creation_ranges_of(self, tier: RescanTier) -> list[DepositCreationRange]:
        """The tier's deposits as creation ranges, each served by an index (``ix_invoices_chain_key_open``,
        ``ix_invoices_created_at_id``). Each pass fixes its own cutoff when it starts, so a deposit that leaves the
        recent window after the running old pass began waits for the next old pass (at most about the old interval
        plus a pass, 24.75 hours in a 2026-10-13 simulation); every deposit is in some pass, none forever skipped."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self.chain.recent_deposit_window_seconds)
        if tier is RescanTier.RECENT:
            # Unclosed invoices from before the cutoff first (a few: they are watched every tick anyway), then every
            # invoice made since it, closed or not.
            return [
                DepositCreationRange(created_before=cutoff, closed=False),
                DepositCreationRange(created_at_or_after=cutoff),
            ]
        return [DepositCreationRange(created_before=cutoff, closed=True)]

    async def rescan_slice(
        self, endpoint: EvmRpcEndpoint, *, rescan_pass: RescanPass, block_heads: BlockHeads, now: float, limit: int
    ) -> int:
        """Up to ``limit`` of the pass's next deposits read and applied; the pass ends (and is logged) once its last
        creation range comes back short. The position moves only once the slice is applied, so a failed slice is read
        again next tick. Returns how many deposits were read."""
        deposits, next_position = await self.list_next_deposits(rescan_pass, limit=limit)
        rescanned = await self.read_rescan_balances(endpoint, deposits=deposits, block_heads=block_heads)
        if rescanned:
            async with self.session_factory() as session, session.begin():
                await self.readings_on(session).apply_rescan(
                    rescanned=rescanned, finalized_block_number=block_heads.finalized.number
                )
        rescan_pass.position = next_position
        rescan_pass.ticks_taken += 1
        rescan_pass.deposits_read += len(deposits)
        rescan_pass.deposits_with_money += sum(1 for deposit in rescanned if any(deposit.balances_by_symbol.values()))
        pass_ended = next_position.creation_range_index >= len(rescan_pass.creation_ranges)
        if pass_ended:
            del self.rescan_pass_by_tier[rescan_pass.tier]
        if rescan_pass.tier is RescanTier.OLD:
            await self.keep_old_rescan_tier_state()
        if not pass_ended:
            return len(deposits)
        pass_seconds = now - rescan_pass.started_at_timestamp
        interval_seconds = self.interval_seconds_of(rescan_pass.tier)
        log.emit(
            logger,
            event=DepositsRescanned(
                chain_key=self.chain.key,
                rescan_tier=rescan_pass.tier.value,
                deposit_count=rescan_pass.deposits_read,
                deposits_with_money=rescan_pass.deposits_with_money,
                tick_count=rescan_pass.ticks_taken,
                pass_seconds=round(pass_seconds, 1),
            ),
        )
        if pass_seconds > interval_seconds:
            log.emit(
                logger,
                event=RescanSlowerThanInterval(
                    chain_key=self.chain.key,
                    rescan_tier=rescan_pass.tier.value,
                    pass_seconds=round(pass_seconds, 1),
                    tier_interval_seconds=interval_seconds,
                ),
            )
        return len(deposits)

    async def list_next_deposits(
        self, rescan_pass: RescanPass, *, limit: int
    ) -> tuple[list[DepositInCreationOrder], RescanPassPosition]:
        """The pass's next ``limit`` deposits, moving on to its next creation range when one runs out, and the
        position past them (the pass itself is left as it is)."""
        deposits: list[DepositInCreationOrder] = []
        position = rescan_pass.position
        async with self.session_factory() as session:
            readings = self.readings_on(session)
            while len(deposits) < limit and position.creation_range_index < len(rescan_pass.creation_ranges):
                wanted = limit - len(deposits)
                page = await readings.list_deposits_in_creation_order(
                    self.chain.key,
                    creation_range=rescan_pass.creation_ranges[position.creation_range_index],
                    after_position=position.last_position,
                    limit=wanted,
                )
                deposits.extend(page)
                if len(page) < wanted:
                    position = RescanPassPosition(creation_range_index=position.creation_range_index + 1)
                else:
                    position = RescanPassPosition(
                        creation_range_index=position.creation_range_index,
                        last_position=DepositCreationPosition(
                            created_at=page[-1].created_at, invoice_id=page[-1].invoice_id
                        ),
                    )
        return deposits, position

    def deposits_per_aggregate3_chunk(self) -> int:
        return max(1, MULTICALL3_CHUNK_SIZE // len(self.chain.assets))

    def rescan_deposits_per_tick(self, deposit_count: int, *, interval_seconds: float) -> int:
        """Deposits a tick so a pass ends in about half its interval, at least one aggregate3 chunk's worth (fewer
        saves nothing); the tick's budget may cut it (``rescan_when_due``)."""
        ticks_per_pass = max(
            1, math.floor(interval_seconds * RESCAN_PASS_SHARE_OF_INTERVAL / self.chain.poll_interval_seconds)
        )
        return max(self.deposits_per_aggregate3_chunk(), math.ceil(deposit_count / ticks_per_pass))

    async def read_rescan_balances(
        self, endpoint: EvmRpcEndpoint, *, deposits: list[DepositInCreationOrder], block_heads: BlockHeads
    ) -> list[RescannedDeposit]:
        calls = [
            encode_native_balance_call(deposit.deposit_address)
            if asset.token_address is None
            else encode_token_balance_call(asset.token_address, holder_address=deposit.deposit_address)
            for deposit in deposits
            for asset in self.chain.assets
        ]
        if not calls:
            return []
        # aggregate3 sends MULTICALL3_CHUNK_SIZE calls per eth_call.
        results = await endpoint.aggregate3(calls, block_hash=block_heads.finalized.hash)
        asset_count = len(self.chain.assets)
        return [
            RescannedDeposit(
                invoice_id=deposit.invoice_id,
                balances_by_symbol={
                    asset.symbol: decode_uint256(result.return_data) if result.succeeded else 0
                    for asset, result in zip(
                        self.chain.assets, results[index * asset_count : (index + 1) * asset_count], strict=True
                    )
                },
            )
            for index, deposit in enumerate(deposits)
        ]
