"""A short-lived copy of each chain's prices, so polling pages do not read the feeds on every request."""

from redis.asyncio import Redis

from app.infrastructure.redis.keys import redis_key
from app.payments.price_quotes.types import ChainPrices

# Polygon's feeds update every half minute or so (chains.toml); PLAN.md section 7.1 caches about 30 s.
PRICE_CACHE_SECONDS = 30
# A read that found no fresh price on any endpoint is not tried again for this long, so invoice pages polling through
# a stalled feed cost one read per chain, not one per endpoint per request (2026-10-14).
PRICE_UNAVAILABLE_CACHE_SECONDS = 10


def chain_prices_key(chain_key: str) -> str:
    return redis_key("prices", "v1", "chain", chain_key)


def chain_prices_unavailable_key(chain_key: str) -> str:
    return redis_key("prices", "v1", "unavailable", chain_key)


class PriceStore:
    def __init__(self, *, client: Redis) -> None:
        self.client = client

    async def find_chain_prices(self, chain_key: str) -> ChainPrices | None:
        cached = await self.client.get(chain_prices_key(chain_key))
        return ChainPrices.model_validate_json(cached) if cached is not None else None

    async def write_chain_prices(self, prices: ChainPrices) -> None:
        await self.client.set(chain_prices_key(prices.chain_key), prices.model_dump_json(), ex=PRICE_CACHE_SECONDS)

    async def is_marked_unavailable(self, chain_key: str) -> bool:
        return bool(await self.client.exists(chain_prices_unavailable_key(chain_key)))

    async def mark_unavailable(self, chain_key: str) -> None:
        await self.client.set(chain_prices_unavailable_key(chain_key), "1", ex=PRICE_UNAVAILABLE_CACHE_SECONDS)
