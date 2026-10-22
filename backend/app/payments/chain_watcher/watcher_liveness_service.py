"""WatcherLivenessService: the API notices a watcher that stopped ticking and alerts the operator (2026-10-12).

The watcher raises every other operator alert, so a dead or stuck watcher raises nothing; the API process, which runs
apart from it, reads the heartbeat the watcher writes to Redis after every completed tick, once a minute
(``WATCHER_LIVENESS_CHECK_INTERVAL_SECONDS``), for every chain the watcher watches: EVM chains with a factory address,
and Monero chains while a sidecar is configured (a Monero tick writes its heartbeat only after it read the sidecar, so
a dead sidecar alerts too; PLAN.md section 14).
A heartbeat older than ``WATCHER_SILENT_ALERT_AFTER_SECONDS``, or none, is an ``operator.alert`` of kind
``watcher_not_ticking`` through ``OperatorAlertRecorder`` (once per chain while it lasts, then at the reminder cadence), which the operator's
/activity shows. Not in the API's first ``WATCHER_SILENT_ALERT_AFTER_SECONDS``: a stack that is starting
gives the watcher that long for its first tick. Redis not answering is logged and checks nothing: the API cannot
tell a dead watcher from a dead Redis, and the watcher's container healthcheck reads the same key.
"""

from collections.abc import Callable
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.chain_watcher.heartbeat_service import WatcherHeartbeatService
from app.payments.chain_watcher.log_events import WatcherLivenessUnreadable
from app.payments.chain_watcher.operator_alert_recorder import OperatorAlertRecorder
from app.payments.chain_watcher.types import ReconciliationFinding
from app.payments.chains.types import ChainConfiguration
from app.shared import logging as log

logger = log.get_logger(__name__)

WATCHER_LIVENESS_CHECK_INTERVAL_SECONDS = 60
# Twice the container healthcheck's limit (WATCHER_HEARTBEAT_MAXIMUM_AGE_SECONDS): a deploy that recreates the watcher
# (about a minute with its lock and RPC checks) does not alert, a watcher down for two minutes does.
WATCHER_SILENT_ALERT_AFTER_SECONDS = 120

type SessionFactory = Callable[[], AsyncSession]


class WatcherLivenessService:
    def __init__(
        self,
        *,
        watched_chains: list[ChainConfiguration],
        redis: Redis,
        session_factory: SessionFactory,
        operator_alert_account_id: int | None,
    ) -> None:
        self.watched_chains = watched_chains
        self.heartbeat_service = WatcherHeartbeatService.on_redis(redis)
        self.operator_alert_recorder = OperatorAlertRecorder(
            session_factory=session_factory, operator_alert_account_id=operator_alert_account_id
        )
        self.started_monotonic = time.monotonic()

    async def check_watcher_heartbeats(self) -> None:
        """One pass: an alert for each watched chain whose watcher has not completed a tick lately."""
        if time.monotonic() - self.started_monotonic < WATCHER_SILENT_ALERT_AFTER_SECONDS:
            return
        for chain in self.watched_chains:
            try:
                seconds_since_tick = await self.heartbeat_service.find_seconds_since_last_tick(chain.key)
            except RedisError:
                log.emit(logger, event=WatcherLivenessUnreadable(chain_key=chain.key))
                continue
            if seconds_since_tick is not None and seconds_since_tick <= WATCHER_SILENT_ALERT_AFTER_SECONDS:
                continue
            stalled_work_text = (
                "no payment is confirmed or credited until it runs again (`docker inspect` the watcher and the "
                "xmr-scanner containers, then their logs)."
                if chain.kind == "monero"
                else "no payment is confirmed, swept or refunded until it runs again (`docker inspect` its container, "
                "then its log)."
            )
            silence_text = (
                "in the last 15 minutes, or ever (no heartbeat in Redis)"
                if seconds_since_tick is None
                else f"for {round(seconds_since_tick / 60)} minutes"
            )
            await self.operator_alert_recorder.record_new_findings(
                [
                    ReconciliationFinding(
                        problem_key=f"watcher_not_ticking:{chain.key}",
                        alert_kind="watcher_not_ticking",
                        invoice_id=None,
                        message=f"The {chain.name} watcher has not completed a tick {silence_text}: {stalled_work_text}",
                        columns={"chain": chain.key},
                    )
                ],
                chain_key=chain.key,
            )
