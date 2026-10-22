"""GCRA rate limiter over Redis.

Each scope keeps one value, a theoretical arrival time. A request pays ``window / limit`` of bucket time and
the bucket tolerates a burst of the whole window. The read-compute-admit is one atomic Lua call, and the key
carries a TTL of its current depth so an idle scope evicts itself.
"""

import math

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.infrastructure.rate_limit.log_events import RateLimitDegraded
from app.infrastructure.redis.keys import redis_key
from app.shared import logging as log

logger = log.get_logger(__name__)

# KEYS[1] = the scope's TAT key. ARGV: [1] window (ms), [2] limit. Returns 0 on admit, else the milliseconds
# until the next slot (at least 1).
GCRA_LUA = """
local server_time = redis.call('TIME')
local now = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local interval = window / limit
local tat = tonumber(redis.call('GET', KEYS[1]) or now)
if tat < now then tat = now end
local new_tat = tat + interval
local allow_at = new_tat - window
if now < allow_at then
  return math.ceil(allow_at - now)
end
redis.call('SET', KEYS[1], new_tat, 'PX', math.ceil(new_tat - now))
return 0
"""


class RateLimiter:
    def __init__(self, client: Redis) -> None:
        self.client = client
        self.script = client.register_script(GCRA_LUA)

    async def seconds_until_admitted(self, scope: str, *, limit: int, window_seconds: int) -> int:
        """Spend one slot of ``scope``'s budget: 0 when admitted, else the whole seconds until its next slot.
        Fails open: a Redis error logs and admits, so a Redis outage never takes the read API down."""
        try:
            wait_milliseconds = int(
                await self.script(keys=[redis_key("rate", scope)], args=[window_seconds * 1000, limit])
            )
        except (RedisError, TimeoutError):
            log.emit(logger, event=RateLimitDegraded(scope_kind=scope.split(":", 1)[0]))
            return 0
        return math.ceil(wait_milliseconds / 1000)
