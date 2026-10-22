"""IpApiKeyRejectionService: whether ip-api rejected the screening key lately, for the API's operator alert check
(2026-10-13).

``SignUpScreeningService`` writes a Redis marker with ip-api's message on every lookup ip-api answers with a key
rejection (``IpApiFailureKind.KEY_REJECTED``), kept ``IP_API_KEY_REJECTION_MARKER_SECONDS``. Screening sits below
the operator alert recorder (``payments.chain_watcher``), so it leaves the marker and the API's
``IpApiKeyAlertService`` reads it here and records the alert.
"""

from typing import Self

from redis.asyncio import Redis

from app.payments.sign_up_screening.store import ScreeningStore


class IpApiKeyRejectionService:
    def __init__(self, *, store: ScreeningStore) -> None:
        self.store = store

    @classmethod
    def on_redis(cls, redis: Redis) -> Self:
        return cls(store=ScreeningStore(client=redis))

    async def find_recent_ip_api_key_rejection(self) -> str | None:
        """ip-api's message of a rejection within the marker's TTL ("" when it sent none), else None. Raises
        ``RedisError``."""
        return await self.store.find_ip_api_key_rejection()
