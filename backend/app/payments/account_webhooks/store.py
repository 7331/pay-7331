"""Pauses of every account's Discord sends, shared by every API process in Redis (2026-10-13; PLAN.md section 8.4).

Discord restricts an IP that makes too many invalid requests: "10,000 per 10 minutes", an invalid request being a
401, 403 or 429, except that "429 errors returned with ``X-RateLimit-Scope: shared`` are not counted against you"
(https://github.com/discord/discord-api-docs/blob/main/developers/topics/rate-limits.mdx, "Invalid Request Limit aka
Cloudflare bans"). So:

- Every such answer counts in a fixed 10-minute window (``INCR``, the key expiring after the window). At
  ``INVALID_DISCORD_REQUESTS_BEFORE_PAUSE`` (2,000) every Discord send pauses until the window ends. A fifth of
  Discord's limit: Discord's own 10 minutes need not line up with ours, so up to twice the count can fall in one of
  its windows (4,000), and the rest is room for anything else on this host's IP that talks to Discord.
- A 429 whose body says ``"global": true`` pauses every send for its ``retry_after``; a 429 without Discord's JSON
  body (not an answer of the API itself, such as a Cloudflare restriction; what one carries is not documented in the
  sources checked) for ``UNEXPLAINED_RATE_LIMIT_PAUSE_SECONDS``, at least its ``Retry-After``.

Copies with a TTL, as everything in Redis here: a lost key only means sends resume. A Redis
error fails open (logged; sends go on), as the rate limiter does: Discord's own answers still pause the account. The
pause is read before every Discord message, so that read gives up after ``PAUSE_READ_TIMEOUT_SECONDS`` (failing open)
rather than the client's 5 s socket timeout: a hung Redis must not stall the sends.
"""

import asyncio
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.infrastructure.redis.keys import redis_key
from app.payments.account_webhooks.log_events import DiscordSendPauseUnavailable, DiscordSendsPaused
from app.shared import logging as log

logger = log.get_logger(__name__)

INVALID_DISCORD_REQUEST_WINDOW_SECONDS = 600
INVALID_DISCORD_REQUESTS_BEFORE_PAUSE = 2000
UNEXPLAINED_RATE_LIMIT_PAUSE_SECONDS = 600.0
PAUSE_READ_TIMEOUT_SECONDS = 0.5
DISCORD_SENDS_PAUSED_KEY = redis_key("account_webhooks", "v1", "discord_sends_paused")


def invalid_discord_requests_key(window_index: int) -> str:
    return redis_key("account_webhooks", "v1", "invalid_discord_requests", window_index)


class DiscordSendPauseStore:
    def __init__(self, *, client: Redis) -> None:
        self.client = client

    async def is_discord_sending_paused(self) -> bool:
        try:
            async with asyncio.timeout(PAUSE_READ_TIMEOUT_SECONDS):
                return bool(await self.client.exists(DISCORD_SENDS_PAUSED_KEY))
        except (RedisError, TimeoutError):
            log.emit(logger, event=DiscordSendPauseUnavailable(step="read"))
            return False

    async def pause_discord_sending(self, *, pause_seconds: float, reason: str) -> None:
        """Pauses every Discord send for ``pause_seconds``; a longer pause already set stays."""
        pause_milliseconds = max(1, round(pause_seconds * 1000))
        try:
            if not await self.client.set(DISCORD_SENDS_PAUSED_KEY, reason, nx=True, px=pause_milliseconds):
                await self.client.pexpire(DISCORD_SENDS_PAUSED_KEY, pause_milliseconds, gt=True)
        except (RedisError, TimeoutError):
            log.emit(logger, event=DiscordSendPauseUnavailable(step="pause"))
            return
        log.emit(logger, event=DiscordSendsPaused(reason=reason, pause_seconds=round(pause_seconds)))

    async def count_invalid_discord_request(self) -> None:
        """Counts one 401, 403 or unshared 429; the count reaching the budget pauses sends until the window ends."""
        now = time.time()
        window_index = int(now // INVALID_DISCORD_REQUEST_WINDOW_SECONDS)
        key = invalid_discord_requests_key(window_index)
        try:
            async with self.client.pipeline(transaction=True) as pipeline:
                pipeline.incr(key)
                pipeline.expire(key, INVALID_DISCORD_REQUEST_WINDOW_SECONDS)
                invalid_request_count, _expire_set = await pipeline.execute()
        except (RedisError, TimeoutError):
            log.emit(logger, event=DiscordSendPauseUnavailable(step="count"))
            return
        if int(invalid_request_count) >= INVALID_DISCORD_REQUESTS_BEFORE_PAUSE:
            window_end = (window_index + 1) * INVALID_DISCORD_REQUEST_WINDOW_SECONDS
            await self.pause_discord_sending(pause_seconds=window_end - now, reason="invalid_request_budget")
