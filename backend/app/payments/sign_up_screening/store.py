"""ip-api answers cached for an hour, keyed by the salted IP hash, and the marker that ip-api rejected the key (PLAN.md
section 9)."""

from redis.asyncio import Redis

from app.infrastructure.ip_api.schemas import IpAddressReputation
from app.infrastructure.redis.keys import redis_key

VERDICT_CACHE_SECONDS = 60 * 60
# Every lookup while the key is rejected writes it again; the API's check reads it once a minute.
IP_API_KEY_REJECTION_MARKER_SECONDS = 10 * 60


def reputation_key(client_ip_hash: str) -> str:
    return redis_key("screening", "v1", "reputation", client_ip_hash)


def ip_api_key_rejection_key() -> str:
    return redis_key("screening", "v1", "ip_api_key_rejected")


class ScreeningStore:
    def __init__(self, *, client: Redis) -> None:
        self.client = client

    async def find_reputation(self, client_ip_hash: str) -> IpAddressReputation | None:
        cached = await self.client.get(reputation_key(client_ip_hash))
        return IpAddressReputation.model_validate_json(cached) if cached is not None else None

    async def write_reputation(self, client_ip_hash: str, *, reputation: IpAddressReputation) -> None:
        await self.client.set(
            reputation_key(client_ip_hash), reputation.model_dump_json(by_alias=True), ex=VERDICT_CACHE_SECONDS
        )

    async def write_ip_api_key_rejection(self, ip_api_message: str) -> None:
        await self.client.set(ip_api_key_rejection_key(), ip_api_message, ex=IP_API_KEY_REJECTION_MARKER_SECONDS)

    async def find_ip_api_key_rejection(self) -> str | None:
        """ip-api's message of the latest rejection within the marker's TTL ("" when it gave none), else None."""
        marker = await self.client.get(ip_api_key_rejection_key())
        if marker is None:
            return None
        return marker.decode() if isinstance(marker, bytes) else marker
