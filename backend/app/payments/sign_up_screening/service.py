"""SignUpScreeningService: the ip-api checks on sign-up, the only screened step (invoices,
payers and API tokens are not screened); verdicts cached by salted IP hash (PLAN.md section 9).

A non-public client address (loopback, private: local development) is admitted without a lookup. Otherwise the
proxy, hosting and country flags apply. ip-api failing (or no key) admits unless
``PAY_SCREENING_FAIL_CLOSED`` is set. ip-api rejecting the key (expired: the pro key is prepaid and renewed by hand)
also leaves a Redis marker, from which the API records an ``operator.alert`` ``ip_api_key_rejected``
(``ip_api_key_rejection_service.py``), so screening does not stay off unnoticed (2026-10-13).
"""

import ipaddress
from typing import Self

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.infrastructure.ip_api.client import IpApiClient
from app.infrastructure.ip_api.errors import IpApiError, IpApiFailureKind
from app.infrastructure.ip_api.schemas import IpAddressReputation
from app.payments.sign_up_screening.configuration import ScreeningSettings
from app.payments.sign_up_screening.errors import SignUpRefusedError, SignUpScreeningUnavailableError
from app.payments.sign_up_screening.log_events import (
    IpApiKeyRejectionNotMarked,
    ScreeningCacheUnavailable,
    SignUpRefused,
    SignUpScreeningLookupFailed,
)
from app.payments.sign_up_screening.store import ScreeningStore
from app.shared import logging as log
from app.shared.client_network import hash_client_network

logger = log.get_logger(__name__)


class SignUpScreeningService:
    def __init__(
        self, *, ip_api_client: IpApiClient | None, store: ScreeningStore, settings: ScreeningSettings
    ) -> None:
        self.ip_api_client = ip_api_client
        self.store = store
        self.settings = settings

    @classmethod
    def on_redis(cls, redis: Redis, *, ip_api_client: IpApiClient | None, settings: ScreeningSettings) -> Self:
        return cls(ip_api_client=ip_api_client, store=ScreeningStore(client=redis), settings=settings)

    def hash_client_ip(self, client_ip_address: str) -> str:
        return hash_client_network(client_ip_address, salt=self.settings.ip_hash_salt.get_secret_value())

    async def screen_client_ip_address(self, client_ip_address: str) -> None:
        """Raise ``SignUpRefusedError`` for an address the flags block."""
        try:
            address = ipaddress.ip_address(client_ip_address)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return
        reputation = await self.read_reputation(client_ip_address) if address is not None else None
        if reputation is None:
            if self.settings.screening_fail_closed:
                raise SignUpScreeningUnavailableError("your network could not be checked; try again later")
            return
        blocked_country_codes = {
            code.strip().upper() for code in self.settings.screening_blocked_country_codes.split(",") if code.strip()
        }
        if self.settings.screening_block_proxy and reputation.proxy:
            self.refuse("proxy", message="sign-ups through VPNs, proxies and Tor are not accepted here")
        if self.settings.screening_block_hosting and reputation.hosting:
            self.refuse("hosting", message="sign-ups from data-centre addresses are not accepted here")
        if reputation.country_code.upper() in blocked_country_codes:
            self.refuse("country", message="sign-ups from your country are not accepted here")

    def refuse(self, reason: str, *, message: str) -> None:
        log.emit(logger, event=SignUpRefused(reason=reason))
        raise SignUpRefusedError(message)

    async def read_reputation(self, client_ip_address: str) -> IpAddressReputation | None:
        """ip-api's answer, cached by IP hash; None when ip-api cannot answer."""
        client_ip_hash = self.hash_client_ip(client_ip_address)
        try:
            cached = await self.store.find_reputation(client_ip_hash)
        except RedisError:
            log.emit(logger, event=ScreeningCacheUnavailable())
            cached = None
        if cached is not None:
            return cached
        if self.ip_api_client is None:
            log.emit(
                logger,
                event=SignUpScreeningLookupFailed(
                    fail_closed=self.settings.screening_fail_closed, reason="no_ip_api_key"
                ),
            )
            return None
        try:
            reputation = await self.ip_api_client.read_ip_address_reputation(client_ip_address)
        except IpApiError as exc:
            log.emit(
                logger,
                event=SignUpScreeningLookupFailed(
                    fail_closed=self.settings.screening_fail_closed,
                    reason=exc.failure_kind.value,
                    ip_api_message=exc.ip_api_message,
                ),
            )
            if exc.failure_kind is IpApiFailureKind.KEY_REJECTED:
                await self.mark_ip_api_key_rejected(exc.ip_api_message or "")
            return None
        try:
            await self.store.write_reputation(client_ip_hash, reputation=reputation)
        except RedisError:
            log.emit(logger, event=ScreeningCacheUnavailable())
        return reputation

    async def mark_ip_api_key_rejected(self, ip_api_message: str) -> None:
        try:
            await self.store.write_ip_api_key_rejection(ip_api_message)
        except RedisError:
            log.emit(logger, event=IpApiKeyRejectionNotMarked())
