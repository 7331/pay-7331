"""MoneroChainWatcherService: one Monero chain's watcher tick (PLAN.md section 14), every ``poll_interval_seconds``.

The scanner is a Rust sidecar that follows the chain and the pool and writes found outputs to ``monero_payments``;
this tick credits them. In order:

1. The sidecar's health read (``MoneroScannerReader.read_chain_health``) gives the tip height and the confirmed tip
   (the lower of the sidecar's tip and a second operator's, which final counting reads). Without an answer, or when
   the sidecar's own scan loop has not completed a tick within 3 poll intervals (``monero_scanner_health.py``: it
   would answer a stale tip), the tick reads and credits nothing (``MoneroScannerUnreadable``,
   ``MoneroScannerStalled``) and writes no heartbeat, so the container healthcheck turns unhealthy once that lasts
   ``MONERO_HEARTBEAT_MAXIMUM_POLL_INTERVALS`` poll intervals. A scan trailing the tip is logged and the tick goes on.
   Unusable wallets (the sidecar holds its scan for them, so a payment on its way may be unseen) are logged and the
   tick goes on without the clock and without the heartbeat (steps 2 and 4), so no invoice expires on a payment
   nobody can see and the API's ``watcher_not_ticking`` fires after 120 s.
2. In one transaction: the open invoices' rows are credited, uncredited after a reorg and counted at final
   (``MoneroReadingsService.apply_monero_payments``), outputs that reached a recently closed invoice are marked late,
   then invoices are expired and closed by the clock (not while a wallet is unusable).
3. After the transaction commits, operator alerts (``OperatorAlertRecorder``, once a problem, then at the reminder cadence): a
   disagreeing check on a second operator (``monero_cross_check_mismatch``, per invoice), and the sidecar findings
   with an alert kind (``monero_scanner_health.find_scanner_loop_problems``: unusable wallets, a confirmed tip that
   lags).
4. The heartbeat, unless a wallet is unusable.

It has no RPC client, no operator key, no sweep and no refund: the receiver's own wallet is the destination. Its reads
grow with the open XMR invoices only, as an EVM tick's do with the watched deposits.
"""

from collections.abc import Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.monero_sidecar_api.errors import MoneroSidecarError, MoneroSidecarInputRefusedError
from app.payments.chain_watcher.heartbeat_service import WatcherHeartbeatService
from app.payments.chain_watcher.log_events import (
    MoneroScannerBehind,
    MoneroScannerStalled,
    MoneroScannerUnreadable,
    MoneroWalletsUnusable,
    WatcherHeartbeatNotWritten,
)
from app.payments.chain_watcher.monero_scanner_health import assess_monero_scanner_health, find_scanner_loop_problems
from app.payments.chain_watcher.operator_alert_recorder import OperatorAlertRecorder
from app.payments.chain_watcher.types import ReconciliationFinding
from app.payments.chains.types import MoneroChainConfiguration
from app.payments.invoices.monero_readings_service import MoneroReadingsService
from app.payments.invoices.wiring import InvoiceWiring
from app.shared import logging as log
from app.shared.logging import LogContext, bound_log_context

logger = log.get_logger(__name__)

# The watcher counts as healthy on a Monero chain while the sidecar answered within this many poll intervals.
MONERO_HEARTBEAT_MAXIMUM_POLL_INTERVALS = 3

type SessionFactory = Callable[[], AsyncSession]


def build_cross_check_mismatch_finding(chain: MoneroChainConfiguration, *, invoice_id: str) -> ReconciliationFinding:
    return ReconciliationFinding(
        problem_key=f"monero_cross_check_mismatch:{invoice_id}",
        alert_kind="monero_cross_check_mismatch",
        invoice_id=invoice_id,
        message=f"A {chain.name} payment to invoice {invoice_id} failed its check on a second node operator (the "
        "transaction, its block or its amount differ), so the invoice is held at confirming and nothing is credited. "
        "Compare the transaction on nodes of two operators and settle it with the receiver.",
        columns={"chain": chain.key, "invoice": invoice_id},
    )


class MoneroChainWatcherService:
    def __init__(
        self,
        *,
        chain: MoneroChainConfiguration,
        session_factory: SessionFactory,
        redis: Redis,
        wiring: InvoiceWiring,
    ) -> None:
        self.chain = chain
        self.session_factory = session_factory
        self.redis = redis
        self.wiring = wiring
        self.heartbeat_service = WatcherHeartbeatService.on_redis(redis)
        self.operator_alert_recorder = OperatorAlertRecorder(
            session_factory=session_factory,
            operator_alert_account_id=wiring.invoice_settings.operator_alert_account_id,
        )

    async def run_tick(self) -> bool:
        """One pass; True: no more work is waiting."""
        with bound_log_context(LogContext(chain_key=self.chain.key)):
            try:
                chain_health = await self.wiring.monero_sidecar.read_chain_health(self.chain.key)
            except (MoneroSidecarError, MoneroSidecarInputRefusedError):
                log.emit(logger, event=MoneroScannerUnreadable(chain_key=self.chain.key))
                return True
            assessment = assess_monero_scanner_health(chain_health, chain=self.chain)
            if assessment.tick_is_stale:
                log.emit(
                    logger,
                    event=MoneroScannerStalled(
                        chain_key=self.chain.key, seconds_since_tick=int(assessment.seconds_since_tick)
                    ),
                )
                return True
            wallets_are_unusable = assessment.unusable_wallet_count > 0
            if wallets_are_unusable:
                log.emit(
                    logger,
                    event=MoneroWalletsUnusable(
                        chain_key=self.chain.key, unusable_wallet_count=assessment.unusable_wallet_count
                    ),
                )
            if assessment.scan_is_behind:
                log.emit(
                    logger,
                    event=MoneroScannerBehind(
                        chain_key=self.chain.key,
                        scanned_height=chain_health.scanned_height,
                        tip_height=chain_health.tip_height,
                    ),
                )
            async with self.session_factory() as session, session.begin():
                readings_service = MoneroReadingsService.on_session(session, redis=self.redis, wiring=self.wiring)
                held_invoice_ids = await readings_service.apply_monero_payments(
                    self.chain, confirmed_tip_height=chain_health.confirmed_tip_height
                )
                await readings_service.apply_late_monero_payments(self.chain)
                if not wallets_are_unusable:
                    await readings_service.expire_and_close_monero_invoices(
                        self.chain, confirmed_tip_height=chain_health.confirmed_tip_height
                    )
            alert_findings = [
                build_cross_check_mismatch_finding(self.chain, invoice_id=invoice_id) for invoice_id in held_invoice_ids
            ] + [
                finding
                for finding in find_scanner_loop_problems(self.chain, chain_health=chain_health)
                if finding.alert_kind is not None
            ]
            if alert_findings:
                await self.operator_alert_recorder.record_new_findings(alert_findings, chain_key=self.chain.key)
            if wallets_are_unusable:
                return True
            try:
                await self.heartbeat_service.record_completed_tick(self.chain.key)
            except RedisError:
                log.emit(logger, event=WatcherHeartbeatNotWritten(chain_key=self.chain.key))
        return True
