"""The Redis client and its lifecycle.

pay-7331 keeps only copies in Redis (rate limit budgets, ip-api verdicts, price reads), so there is
no durable/cache split and no eviction check. The client is binary-safe and speaks RESP3.
"""

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import EqualJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

from app.infrastructure.redis.log_events import RedisClosed, RedisInitialized
from app.shared import logging as log

logger = log.get_logger(__name__)


class RedisClient:
    """One Redis connection pool with its lifecycle."""

    def __init__(self, dsn: str, *, client_name: str) -> None:
        pool = BlockingConnectionPool.from_url(
            dsn,
            max_connections=50,
            timeout=2.0,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
            socket_keepalive=True,
            health_check_interval=30,
            # Only ConnectionError is retried, not TimeoutError. redis-py also raises ConnectionError on a read that
            # fails after the command was sent, so a retried command may have run twice: at worst one extra INCR count
            # or GCRA slot, which only makes a rate limit stricter for a moment.
            retry=Retry(EqualJitterBackoff(base=0.05, cap=1.0), 3, supported_errors=(RedisConnectionError,)),
            decode_responses=False,
            protocol=3,
            client_name=client_name,
        )
        self.client = Redis.from_pool(pool)

    async def initialize(self) -> None:
        await self.client.ping()
        log.emit(logger, event=RedisInitialized())

    async def close(self) -> None:
        await self.client.aclose()
        log.emit(logger, event=RedisClosed())
