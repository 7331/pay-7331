"""What the watcher and ``reconcile`` make of the scanner sidecar's health for one Monero chain (PLAN.md section 14).

The sidecar answers ``/healthz`` from memory, so an answer alone does not mean its scan loop is running: a loop that
stopped completing ticks keeps its last tip. Three readings beyond "it answered":

- stale: its last completed tick is older than ``MONERO_SCANNER_TICK_MAXIMUM_POLL_INTERVALS`` poll intervals. The
  watcher then treats the sidecar as unreadable: it credits nothing and writes no heartbeat.
- behind: its scanned height trails its tip by more than ``MONERO_SCAN_LAG_WARNING_BLOCKS`` (catching up after an
  outage, or holding for an unusable wallet). A warning: rows already written stay correct to credit.
- unusable wallets: a watched wallet whose sealed view key does not open or match; the sidecar holds its cursor
  until it is fixed. The watcher then neither expires nor closes the chain's invoices (a payment it cannot see may
  be on its way) and writes no heartbeat, so ``watcher_not_ticking`` fires too; and an operator alert
  (``monero_wallets_unusable``).
- confirmed tip lagging: the confirmed tip (the lower of the sidecar's tip and another operator's) has trailed the
  tip by more than 3 blocks, or been unknown, for over ``MONERO_SCANNER_TICK_MAXIMUM_POLL_INTERVALS`` poll intervals:
  nothing becomes final meanwhile. An operator alert (``monero_confirmed_tip_lagging``).

``find_scanner_loop_problems`` turns these into findings for ``reconcile``; the watcher records those with an
``alert_kind`` each tick through ``OperatorAlertRecorder`` (once a problem, then at the reminder cadence).
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from app.infrastructure.monero_sidecar_api.schemas import MoneroChainHealth
from app.payments.chain_watcher.types import ReconciliationFinding
from app.payments.chains.types import MoneroChainConfiguration

# The sidecar's own `ok` uses the same 3 poll intervals (xmr-scanner/src/chain_health.rs).
MONERO_SCANNER_TICK_MAXIMUM_POLL_INTERVALS = 3
# About 20 minutes of mainnet blocks.
MONERO_SCAN_LAG_WARNING_BLOCKS = 10


class MoneroScannerAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    seconds_since_tick: float
    maximum_tick_age_seconds: float
    scan_lag_blocks: int | None
    unusable_wallet_count: int
    seconds_confirmed_tip_lagging: float | None

    @property
    def tick_is_stale(self) -> bool:
        return self.seconds_since_tick > self.maximum_tick_age_seconds

    @property
    def confirmed_tip_is_lagging(self) -> bool:
        return (
            self.seconds_confirmed_tip_lagging is not None
            and self.seconds_confirmed_tip_lagging > self.maximum_tick_age_seconds
        )

    @property
    def scan_is_behind(self) -> bool:
        return self.scan_lag_blocks is None or self.scan_lag_blocks > MONERO_SCAN_LAG_WARNING_BLOCKS


def assess_monero_scanner_health(
    chain_health: MoneroChainHealth, *, chain: MoneroChainConfiguration
) -> MoneroScannerAssessment:
    now = datetime.now(UTC)
    return MoneroScannerAssessment(
        seconds_since_tick=max((now - chain_health.last_completed_tick_at).total_seconds(), 0.0),
        maximum_tick_age_seconds=MONERO_SCANNER_TICK_MAXIMUM_POLL_INTERVALS * chain.poll_interval_seconds,
        scan_lag_blocks=None
        if chain_health.scanned_height is None
        else max(chain_health.tip_height - chain_health.scanned_height, 0),
        unusable_wallet_count=chain_health.unusable_wallet_count,
        seconds_confirmed_tip_lagging=None
        if chain_health.confirmed_tip_lagging_since is None
        else max((now - chain_health.confirmed_tip_lagging_since).total_seconds(), 0.0),
    )


def find_scanner_loop_problems(
    chain: MoneroChainConfiguration, *, chain_health: MoneroChainHealth
) -> list[ReconciliationFinding]:
    assessment = assess_monero_scanner_health(chain_health, chain=chain)
    findings: list[ReconciliationFinding] = []
    if assessment.tick_is_stale:
        findings.append(
            ReconciliationFinding(
                problem_key=f"monero_scanner_stalled:{chain.key}",
                alert_kind=None,
                invoice_id=None,
                message=f"The {chain.name} scanner sidecar answers, but its scan loop last completed a tick "
                f"{assessment.seconds_since_tick:.0f} s ago: nothing is credited until it ticks again. Check its log "
                "for monero_scan_tick_failed.",
                columns={"chain": chain.key, "problem": "scanner_stalled"},
            )
        )
    if assessment.unusable_wallet_count > 0:
        findings.append(
            ReconciliationFinding(
                problem_key=f"monero_wallets_unusable:{chain.key}",
                alert_kind="monero_wallets_unusable",
                invoice_id=None,
                message=f"The {chain.name} scanner cannot open or match the view key of "
                f"{assessment.unusable_wallet_count} watched wallets and holds its scan for them: no new payment is "
                "seen on this chain and no invoice of it expires until they are fixed. Its log names them "
                "(monero_wallet_unusable); a changed PAY_INVOICE_SALT_SEED is the usual cause.",
                columns={
                    "chain": chain.key,
                    "problem": "wallets_unusable",
                    "unusable_wallets": str(assessment.unusable_wallet_count),
                },
            )
        )
    if assessment.confirmed_tip_is_lagging:
        findings.append(
            ReconciliationFinding(
                problem_key=f"monero_confirmed_tip_lagging:{chain.key}",
                alert_kind="monero_confirmed_tip_lagging",
                invoice_id=None,
                message=f"The {chain.name} confirmed tip ({chain_health.confirmed_tip_height}) has trailed the tip "
                f"({chain_health.tip_height}) by more than 3 blocks, or been unknown, for "
                f"{assessment.seconds_confirmed_tip_lagging or 0:.0f} s: no node of a second operator keeps up, so no "
                "payment on this chain becomes final. Fix or replace nodes in chains.toml or `PAY_MONERO_NODES_<CHAIN>`.",
                columns={
                    "chain": chain.key,
                    "problem": "confirmed_tip_lagging",
                    "tip": str(chain_health.tip_height),
                    "confirmed_tip": str(chain_health.confirmed_tip_height),
                },
            )
        )
    if assessment.scan_is_behind:
        findings.append(
            ReconciliationFinding(
                problem_key=f"monero_scanner_behind:{chain.key}",
                alert_kind=None,
                invoice_id=None,
                message=f"The {chain.name} scanner has scanned to block {chain_health.scanned_height}, more than "
                f"{MONERO_SCAN_LAG_WARNING_BLOCKS} blocks behind its tip {chain_health.tip_height}: payments in the "
                "blocks between are not seen yet.",
                columns={"chain": chain.key, "problem": "scanner_behind"},
            )
        )
    return findings
