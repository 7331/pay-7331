"""AccountEventStreamSlotStore: how many event streams one session holds open, across every API process.

The API runs several worker processes (``--workers`` in docker-compose.yml), so an in-process count would let a
session open that many streams on each; the count lives in Redis instead. Each session has one sorted set whose members
are its open streams and whose scores are each stream's lease end (milliseconds, Redis server time). Opening a stream
drops the expired leases, counts the rest and adds its own only below ``MAXIMUM_OPEN_STREAMS_PER_ACCOUNT_SESSION``,
in one Lua call so two opens cannot both take the last slot. An open stream renews its lease every
``STREAM_SLOT_RENEW_INTERVAL_SECONDS`` and removes it when it ends; a process killed mid-stream leaves a lease that
lapses within ``STREAM_SLOT_LEASE_SECONDS``, and the key itself expires with its newest lease.

Commands: ``ZREMRANGEBYSCORE`` (https://redis.io/docs/latest/commands/zremrangebyscore/), ``ZCARD``
(https://redis.io/docs/latest/commands/zcard/), ``ZADD`` (https://redis.io/docs/latest/commands/zadd/), ``ZREM``
(https://redis.io/docs/latest/commands/zrem/) and ``PEXPIRE`` (https://redis.io/docs/latest/commands/pexpire/), with
``TIME`` in the script (https://redis.io/docs/latest/develop/programmability/eval-intro/: scripts are replicated as
their effects, so a script may read the clock).

A Redis error fails open (logged; the stream opens), as the rate limiter does: the cap is an abuse backstop, and a Redis
outage must not take the activity page down with it.
"""

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.infrastructure.redis.keys import redis_key
from app.payments.account_events.log_events import AccountEventStreamSlotsUnavailable
from app.shared import logging as log

logger = log.get_logger(__name__)

MAXIMUM_OPEN_STREAMS_PER_ACCOUNT_SESSION = 3
STREAM_SLOT_LEASE_SECONDS = 60
STREAM_SLOT_RENEW_INTERVAL_SECONDS = 20

# KEYS[1] = the session's sorted set. ARGV: [1] the stream's slot id, [2] lease (ms), [3] most open streams, [4] 1 to
# claim a slot (refused at the cap), 0 to renew one (re-added if it lapsed). Returns 1 when the slot is held, else 0.
CLAIM_OR_RENEW_STREAM_SLOT_LUA = """
local server_time = redis.call('TIME')
local now = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local lease = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if ARGV[4] == '1' and redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
  return 0
end
redis.call('ZADD', KEYS[1], now + lease, ARGV[1])
redis.call('PEXPIRE', KEYS[1], lease)
return 1
"""


def account_session_stream_slots_key(account_session_id: int) -> str:
    return redis_key("account_events", "v1", "stream_slots", account_session_id)


class AccountEventStreamSlotStore:
    def __init__(self, *, client: Redis) -> None:
        self.client = client
        self.script = client.register_script(CLAIM_OR_RENEW_STREAM_SLOT_LUA)

    async def claim_stream_slot(self, account_session_id: int, *, stream_slot_id: str) -> bool:
        """Takes one of the session's stream slots: False when all are held."""
        return await self.run_slot_script(account_session_id, stream_slot_id=stream_slot_id, claim=True, step="claim")

    async def renew_stream_slot(self, account_session_id: int, *, stream_slot_id: str) -> None:
        await self.run_slot_script(account_session_id, stream_slot_id=stream_slot_id, claim=False, step="renew")

    async def release_stream_slot(self, account_session_id: int, *, stream_slot_id: str) -> None:
        try:
            await self.client.zrem(account_session_stream_slots_key(account_session_id), stream_slot_id)
        except (RedisError, TimeoutError):
            log.emit(logger, event=AccountEventStreamSlotsUnavailable(step="release"))

    async def run_slot_script(self, account_session_id: int, *, stream_slot_id: str, claim: bool, step: str) -> bool:
        try:
            slot_held = await self.script(
                keys=[account_session_stream_slots_key(account_session_id)],
                args=[
                    stream_slot_id,
                    STREAM_SLOT_LEASE_SECONDS * 1000,
                    MAXIMUM_OPEN_STREAMS_PER_ACCOUNT_SESSION,
                    1 if claim else 0,
                ],
            )
        except (RedisError, TimeoutError):
            log.emit(logger, event=AccountEventStreamSlotsUnavailable(step=step))
            return True
        return int(slot_held) == 1
