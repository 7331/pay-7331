"""``reconcile --chain <monero chain>``'s scanner check (PLAN.md section 14), read only.

The sidecar's health read: its tip and how many node operators answer it. Under ``MINIMUM_HEALTHY_MONERO_OPERATORS``
no payment can be cross-checked, so credits wait. The sidecar's own loop (``monero_scanner_health.py``): a last
completed tick older than 3 poll intervals (stalled: it answers an old tip), a scan trailing the tip, and watched
wallets it cannot scan (it holds its cursor for them) are findings too. With the watcher's heartbeat: a Monero tick
completes only when the sidecar answered with a fresh tick, so a stale one means the watcher or the sidecar stopped.
With no sidecar configured (``PAY_MONERO_SIDECAR_URL`` empty) the chain is idle on purpose: the check says so in its
summary, a warning with no finding, and reads neither the sidecar nor the heartbeat.
"""

from redis.exceptions import RedisError

from app.infrastructure.monero_sidecar_api.errors import MoneroSidecarError, MoneroSidecarInputRefusedError
from app.payments.chain_watcher.heartbeat_service import WatcherHeartbeatService
from app.payments.chain_watcher.monero_scanner_health import find_scanner_loop_problems
from app.payments.chain_watcher.monero_service import MONERO_HEARTBEAT_MAXIMUM_POLL_INTERVALS
from app.payments.chain_watcher.types import ReconciliationCheckResult, ReconciliationFinding
from app.payments.chains.types import MoneroChainConfiguration
from app.payments.monero_wallets.types import MoneroScannerReader

# A credit needs a second operator to agree (PLAN.md section 14).
MINIMUM_HEALTHY_MONERO_OPERATORS = 2


async def check_monero_scanner(
    chain: MoneroChainConfiguration,
    *,
    scanner_reader: MoneroScannerReader,
    heartbeat_service: WatcherHeartbeatService,
    sidecar_is_configured: bool,
) -> ReconciliationCheckResult:
    if not sidecar_is_configured:
        return ReconciliationCheckResult(
            check_name="monero_scanner",
            summary=f"{chain.name} is idle: no wallet can be set, no XMR invoice made and nothing credited",
            findings=[],
            warning="no sidecar configured (PAY_MONERO_SIDECAR_URL empty)",
        )
    findings: list[ReconciliationFinding] = []
    summary_parts: list[str] = []
    try:
        chain_health = await scanner_reader.read_chain_health(chain.key)
    except (MoneroSidecarError, MoneroSidecarInputRefusedError):
        chain_health = None
        summary_parts.append("the scanner sidecar did not answer")
        findings.append(
            ReconciliationFinding(
                problem_key=f"monero_scanner_unreadable:{chain.key}",
                alert_kind=None,
                invoice_id=None,
                message=f"The {chain.name} scanner sidecar did not answer: no XMR invoice can be made and nothing is "
                "credited until it does.",
                columns={"chain": chain.key, "problem": "sidecar_unreadable"},
            )
        )
    if chain_health is not None:
        summary_parts.append(
            f"tip {chain_health.tip_height} (confirmed {chain_health.confirmed_tip_height}), scanned to "
            f"{chain_health.scanned_height}, {chain_health.healthy_operator_count} healthy node operators"
        )
        findings.extend(find_scanner_loop_problems(chain, chain_health=chain_health))
        if chain_health.healthy_operator_count < MINIMUM_HEALTHY_MONERO_OPERATORS:
            findings.append(
                ReconciliationFinding(
                    problem_key=f"monero_operators_too_few:{chain.key}",
                    alert_kind=None,
                    invoice_id=None,
                    message=f"The {chain.name} scanner has {chain_health.healthy_operator_count} healthy node "
                    f"operators, under {MINIMUM_HEALTHY_MONERO_OPERATORS}: payments cannot be cross-checked, so credits "
                    "wait. Fix or replace nodes in chains.toml or `PAY_MONERO_NODES_<CHAIN>`.",
                    columns={
                        "chain": chain.key,
                        "problem": "operators_too_few",
                        "healthy_operators": str(chain_health.healthy_operator_count),
                    },
                )
            )
    maximum_age_seconds = MONERO_HEARTBEAT_MAXIMUM_POLL_INTERVALS * chain.poll_interval_seconds
    try:
        seconds_since_tick = await heartbeat_service.find_seconds_since_last_tick(chain.key)
    except RedisError:
        seconds_since_tick = None
        summary_parts.append("heartbeat unreadable")
    else:
        summary_parts.append(
            "no completed tick" if seconds_since_tick is None else f"last tick {seconds_since_tick:.0f} s ago"
        )
    if seconds_since_tick is None or seconds_since_tick > maximum_age_seconds:
        findings.append(
            ReconciliationFinding(
                problem_key=f"monero_watcher_silent:{chain.key}",
                alert_kind=None,
                invoice_id=None,
                message=f"The {chain.name} watcher has not completed a tick with the scanner answering within "
                f"{maximum_age_seconds:.0f} s: check the watcher and the sidecar containers' logs.",
                columns={"chain": chain.key, "problem": "watcher_silent"},
            )
        )
    return ReconciliationCheckResult(check_name="monero_scanner", summary=", ".join(summary_parts), findings=findings)
