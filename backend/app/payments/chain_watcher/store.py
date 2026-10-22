"""The watcher's copies in Redis: its heartbeat, one key per chain written after every completed tick (2026-10-12),
and the old rescan tier's running pass, one key per chain written after every applied slice (2026-10-13).

A copy with a TTL, as everything in Redis here: ``SET`` with ``EX`` replaces the value and
its time to live on every write (https://redis.io/docs/latest/commands/set/), so a watcher that stops ticking leaves a
key that ages and then disappears. Redis rather than a file in the container: ``reconcile`` (run in another
container) reads it too, and the watcher container already reaches Redis on the stack's internal network.
"""

from pydantic import ValidationError
from redis.asyncio import Redis

from app.infrastructure.redis.keys import redis_key
from app.payments.chain_watcher.types import OldRescanTierState, WatcherHeartbeat

# Long past the healthcheck's limit, so `reconcile` can still say how long ago the last tick was.
WATCHER_HEARTBEAT_TTL_SECONDS = 900


def watcher_heartbeat_key(chain_key: str) -> str:
    return redis_key("watcher", "v1", "heartbeat", chain_key)


class WatcherHeartbeatStore:
    def __init__(self, *, client: Redis) -> None:
        self.client = client

    async def write_heartbeat(self, heartbeat: WatcherHeartbeat) -> None:
        await self.client.set(
            watcher_heartbeat_key(heartbeat.chain_key), heartbeat.model_dump_json(), ex=WATCHER_HEARTBEAT_TTL_SECONDS
        )

    async def find_heartbeat(self, chain_key: str) -> WatcherHeartbeat | None:
        stored = await self.client.get(watcher_heartbeat_key(chain_key))
        return WatcherHeartbeat.model_validate_json(stored) if stored is not None else None


def old_rescan_tier_state_key(chain_key: str) -> str:
    return redis_key("watcher", "v1", "old_rescan_tier_state", chain_key)


class OldRescanTierStateStore:
    def __init__(self, *, client: Redis) -> None:
        self.client = client

    async def write_old_rescan_tier_state(self, state: OldRescanTierState, *, ttl_seconds: int) -> None:
        await self.client.set(old_rescan_tier_state_key(state.chain_key), state.model_dump_json(), ex=ttl_seconds)

    async def find_old_rescan_tier_state(self, chain_key: str) -> OldRescanTierState | None:
        """None when no state is kept, or one this code no longer reads (the next pass then starts afresh)."""
        stored = await self.client.get(old_rescan_tier_state_key(chain_key))
        if stored is None:
            return None
        try:
            return OldRescanTierState.model_validate_json(stored)
        except ValidationError:
            return None
