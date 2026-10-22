"""WatcherHeartbeatService: "a tick of this chain just completed" (2026-10-12).

The watcher writes it after every completed tick; ``python -m app.watcher_healthcheck`` (the watcher container's
compose healthcheck) and ``reconcile`` read its age.
"""

import time
from typing import Self

from redis.asyncio import Redis

from app.payments.chain_watcher.store import WatcherHeartbeatStore
from app.payments.chain_watcher.types import WatcherHeartbeat

# A tick runs every poll interval (1-2 s) and gives up after TICK_TIMEOUT_SECONDS (90 s); a minute with no completed
# tick means every endpoint is failing or the loop is stuck.
WATCHER_HEARTBEAT_MAXIMUM_AGE_SECONDS = 60


class WatcherHeartbeatService:
    def __init__(self, *, store: WatcherHeartbeatStore) -> None:
        self.store = store

    @classmethod
    def on_redis(cls, redis: Redis) -> Self:
        return cls(store=WatcherHeartbeatStore(client=redis))

    async def record_completed_tick(self, chain_key: str) -> None:
        await self.store.write_heartbeat(WatcherHeartbeat(chain_key=chain_key, completed_at_timestamp=time.time()))

    async def find_seconds_since_last_tick(self, chain_key: str) -> float | None:
        """None when no tick completed within the key's TTL (or ever)."""
        heartbeat = await self.store.find_heartbeat(chain_key)
        return max(0.0, time.time() - heartbeat.completed_at_timestamp) if heartbeat is not None else None
