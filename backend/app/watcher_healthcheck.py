"""The watcher container's compose healthcheck: ``python -m app.watcher_healthcheck`` (2026-10-12).

Exit 0 while every EVM chain the watcher watches (enabled, with a factory address) completed a tick within
``WATCHER_HEARTBEAT_MAXIMUM_AGE_SECONDS`` and every Monero chain within ``MONERO_HEARTBEAT_MAXIMUM_POLL_INTERVALS``
poll intervals (its tick completes only when the scanner sidecar answered; PLAN.md section 14), else 1. With
``PAY_MONERO_SIDECAR_URL`` empty the watcher runs no Monero loop, so Monero chains are skipped here. It reads the
heartbeat the watcher writes to Redis after every completed tick, over the stack's internal network the container is
already on; no other network access. Docker keeps the last lines it prints in ``docker inspect``'s health log
(https://docs.docker.com/reference/compose-file/services/#healthcheck).
"""

import asyncio
import sys

from redis.exceptions import RedisError

from app.config import read_application_settings
from app.infrastructure.monero_sidecar_api.configuration import monero_sidecar_is_configured
from app.infrastructure.redis.client import RedisClient
from app.payments.chain_watcher.heartbeat_service import (
    WATCHER_HEARTBEAT_MAXIMUM_AGE_SECONDS,
    WatcherHeartbeatService,
)
from app.payments.chain_watcher.monero_service import MONERO_HEARTBEAT_MAXIMUM_POLL_INTERVALS
from app.payments.chains.configuration import read_enabled_chains
from app.payments.chains.types import EvmChainConfiguration, MoneroChainConfiguration
from app.shared.logging import configure_logging


def list_maximum_heartbeat_age_seconds_by_chain_key() -> dict[str, float]:
    """Per chain the watcher runs a loop for, the oldest its last completed tick may be; a Monero chain with no sidecar
    configured is reported idle and left out."""
    maximum_age_seconds_by_chain_key: dict[str, float] = {}
    for chain in read_enabled_chains():
        if isinstance(chain, EvmChainConfiguration) and chain.factory_address is not None:
            maximum_age_seconds_by_chain_key[chain.key] = WATCHER_HEARTBEAT_MAXIMUM_AGE_SECONDS
        if isinstance(chain, MoneroChainConfiguration):
            if not monero_sidecar_is_configured():
                sys.stdout.write(f"{chain.key}: idle, no sidecar configured (PAY_MONERO_SIDECAR_URL empty)\n")
                continue
            maximum_age_seconds_by_chain_key[chain.key] = (
                MONERO_HEARTBEAT_MAXIMUM_POLL_INTERVALS * chain.poll_interval_seconds
            )
    return maximum_age_seconds_by_chain_key


async def check_watcher_heartbeats() -> bool:
    maximum_age_seconds_by_chain_key = list_maximum_heartbeat_age_seconds_by_chain_key()
    redis_client = RedisClient(read_application_settings().redis_url, client_name="pay-watcher-healthcheck")
    try:
        heartbeat_service = WatcherHeartbeatService.on_redis(redis_client.client)
        every_chain_ticking = True
        for chain_key, maximum_age_seconds in maximum_age_seconds_by_chain_key.items():
            seconds_since_tick = await heartbeat_service.find_seconds_since_last_tick(chain_key)
            ticking = seconds_since_tick is not None and seconds_since_tick <= maximum_age_seconds
            last_tick_text = "no tick recorded" if seconds_since_tick is None else f"{seconds_since_tick:.0f} s ago"
            sys.stdout.write(f"{chain_key}: last completed tick {last_tick_text}\n")
            every_chain_ticking = every_chain_ticking and ticking
    except RedisError as error:
        sys.stdout.write(f"redis did not answer: {type(error).__name__}\n")
        return False
    finally:
        await redis_client.close()
    return every_chain_ticking


if __name__ == "__main__":
    # Its own log lines (the Redis client's) are not health output.
    configure_logging("WARNING")
    sys.exit(0 if asyncio.run(check_watcher_heartbeats()) else 1)
