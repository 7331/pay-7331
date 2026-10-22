"""OperatorAlertService: one chain's after-tick duties that tell the operator something is wrong (2026-10-12).

After every completed tick it writes the watcher heartbeat (the container healthcheck reads it). Every
``OPERATOR_ALERT_CHECK_INTERVAL_SECONDS`` it runs ``ReconciliationService.run_alert_checks`` (a stuck or failed sweep,
a refund needing the owner, refunds waiting for a float that cannot pay them, a stale or unreadable price feed, the
watcher behind the finalized head, Postgres's WAL archiving failing or stalled), adds the static deposit scan's
slow tiers, and hands the findings to
``OperatorAlertRecorder``: an ``operator.alert`` account event on the operator's account when a problem opens, then at
its Discord reminder cadence. Its /activity shows it over the account event stream, and its Discord webhook gets it
(PLAN.md section 8.4). A failure here is
logged and never fails or slows the tick's own work. That the watcher stopped is noticed by the API
(``WatcherLivenessService``), since a dead watcher raises nothing.

Before each settlement pass, ``maintain_operator_keys`` runs the chain's ``OperatorKeyMaintenanceService`` (once a
minute: the leak check, top-ups from the treasury Safe, retired keys' returns; 2026-10-13) and alerts its findings the
same way. It runs before the pass so a key used elsewhere is frozen before the pass may sign with it; a failure is
logged and the pass goes on.
"""

import asyncio
from collections.abc import Callable
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.schemas import BlockHeads
from app.payments.chain_watcher.heartbeat_service import WatcherHeartbeatService
from app.payments.chain_watcher.log_events import (
    OperatorAlertCheckFailed,
    OperatorKeyMaintenanceFailed,
    WatcherHeartbeatNotWritten,
)
from app.payments.chain_watcher.operator_alert_recorder import OperatorAlertRecorder
from app.payments.chain_watcher.reconciliation_service import ReconciliationService
from app.payments.chain_watcher.types import ReconciliationFinding
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.settlements.operator_key_maintenance_service import OperatorKeyMaintenanceService
from app.payments.settlements.runner_service import SettlementRunner
from app.payments.settlements.types import OperatorKeyFinding
from app.shared import logging as log

logger = log.get_logger(__name__)

# About 30 ticks on Polygon: a few RPC reads and indexed queries a minute.
OPERATOR_ALERT_CHECK_INTERVAL_SECONDS = 60
OPERATOR_ALERT_CHECK_TIMEOUT_SECONDS = 60
# An operation whose step failed this many passes in a row (about 20 s on Polygon) is stuck, not unlucky: since an
# outcome commits with its effect on the invoice, a failing effect keeps it unresolved and only logs ERROR each pass.
REPEATED_STEP_FAILURE_ALERT_PASSES = 10
# It runs inside a tick: a slow endpoint may hold the tick this long, once a minute at most.
OPERATOR_KEY_MAINTENANCE_TIMEOUT_SECONDS = 20

type SessionFactory = Callable[[], AsyncSession]


def reconciliation_finding_of(finding: OperatorKeyFinding) -> ReconciliationFinding:
    return ReconciliationFinding(
        problem_key=finding.problem_key,
        alert_kind=finding.alert_kind,
        invoice_id=None,
        message=finding.message,
        columns=finding.columns,
    )


class OperatorAlertService:
    def __init__(
        self,
        *,
        chain: EvmChainConfiguration,
        session_factory: SessionFactory,
        redis: Redis,
        wiring: InvoiceWiring,
        settlement_runner: SettlementRunner,
        operator_key_maintenance_service: OperatorKeyMaintenanceService,
    ) -> None:
        self.chain = chain
        self.settlement_runner = settlement_runner
        self.operator_key_maintenance_service = operator_key_maintenance_service
        self.session_factory = session_factory
        self.redis = redis
        self.wiring = wiring
        self.heartbeat_service = WatcherHeartbeatService.on_redis(redis)
        self.operator_alert_recorder = OperatorAlertRecorder(
            session_factory=session_factory,
            operator_alert_account_id=wiring.invoice_settings.operator_alert_account_id,
        )
        self.last_check_monotonic: float | None = None

    async def after_completed_tick(self, *, static_deposit_scan_findings: list[ReconciliationFinding]) -> None:
        """``static_deposit_scan_findings``: the static deposit scan's slow tiers (in memory, no read), alerted with
        the minute check's findings."""
        try:
            await self.heartbeat_service.record_completed_tick(self.chain.key)
        except RedisError:
            log.emit(logger, event=WatcherHeartbeatNotWritten(chain_key=self.chain.key))
        now = time.monotonic()
        if (
            self.last_check_monotonic is not None
            and now - self.last_check_monotonic < OPERATOR_ALERT_CHECK_INTERVAL_SECONDS
        ):
            return
        self.last_check_monotonic = now
        # Broad on purpose: an alert check that fails must never stop the watcher's loop or its next tick.
        try:
            async with asyncio.timeout(OPERATOR_ALERT_CHECK_TIMEOUT_SECONDS):
                await self.raise_operator_alerts(static_deposit_scan_findings=static_deposit_scan_findings)
        except Exception:
            log.exception(logger, event=OperatorAlertCheckFailed(chain_key=self.chain.key))

    async def maintain_operator_keys(self, endpoint: EvmRpcEndpoint, *, block_heads: BlockHeads) -> None:
        # Broad on purpose, as above: the settlement pass after it must run whatever happens here.
        try:
            async with asyncio.timeout(OPERATOR_KEY_MAINTENANCE_TIMEOUT_SECONDS):
                findings = await self.operator_key_maintenance_service.run_when_due(endpoint, block_heads=block_heads)
                if findings:
                    await self.operator_alert_recorder.record_new_findings(
                        [reconciliation_finding_of(finding) for finding in findings if finding.alert_kind is not None],
                        chain_key=self.chain.key,
                    )
        except Exception:
            log.exception(logger, event=OperatorKeyMaintenanceFailed(chain_key=self.chain.key))

    async def raise_operator_alerts(self, *, static_deposit_scan_findings: list[ReconciliationFinding]) -> None:
        async with self.session_factory() as session:
            check_results = await ReconciliationService.on_session(
                session, redis=self.redis, wiring=self.wiring
            ).run_alert_checks(self.chain)
        await self.operator_alert_recorder.record_new_findings(
            [
                *(finding for result in check_results for finding in result.findings),
                *self.find_repeated_step_failures(),
                *static_deposit_scan_findings,
            ],
            chain_key=self.chain.key,
        )

    def find_repeated_step_failures(self) -> list[ReconciliationFinding]:
        return [
            ReconciliationFinding(
                problem_key=f"operation_step_failing:{operation_id}",
                alert_kind="operation_step_failing",
                invoice_id=None,
                message=f"Operation {operation_id} on {self.chain.name} failed its step {failures} passes in a row; "
                "it stays unresolved until the cause is fixed (watcher log `settlement_step`).",
                columns={"operation": str(operation_id), "failed passes": str(failures)},
            )
            for operation_id, failures in self.settlement_runner.find_operations_failing_repeatedly(
                self.chain.key, minimum_failures=REPEATED_STEP_FAILURE_ALERT_PASSES
            ).items()
        ]
