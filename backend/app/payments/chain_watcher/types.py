"""The watcher's own read types: what one tick works out before it writes (PLAN.md section 5)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.payments.chain_watcher.enums import RescanTier
from app.payments.invoices.types import (
    AttributedPayment,
    AttributedStrayPayment,
    CanonicalTransactionPayments,
    DepositCreationPosition,
    DepositCreationRange,
    FinalPaymentBlock,
    WatchedDeposit,
)
from app.payments.static_deposit_addresses.types import ScannedStaticDeposit


class ResolvedFinality(BaseModel):
    """What the canonical chain says about rows whose block is now final: rows confirmed where they are, transactions
    rebuilt from their receipts, and rows with no transaction whose block left the chain."""

    confirmed: dict[int, FinalPaymentBlock]
    canonical_transactions: list[CanonicalTransactionPayments]
    rows_off_chain: list[int]


class NativeAttribution(BaseModel):
    """Native money into watched deposits found this tick: payments of native invoices, and native stray payments
    into token invoices' deposits (INC-001)."""

    payments: list[AttributedPayment]
    stray_payments: list[AttributedStrayPayment]


class NativeRiseSelection(BaseModel):
    """The watched deposits whose native balance rose, and from which block each range of native attribution reads:
    ``start_blocks`` back from ``latest``, ``finalized_start_blocks`` back from the finalized head. A token invoice's
    deposit is in ``stray_deposit_by_invoice`` too (INC-001)."""

    model_config = ConfigDict(frozen=True)

    invoice_by_deposit: dict[str, str]
    stray_deposit_by_invoice: dict[str, WatchedDeposit]
    start_blocks: list[int]
    finalized_start_blocks: list[int]


class RescanPassPosition(BaseModel):
    """How far a rescan pass has applied: the creation range it is in and the last deposit of it read."""

    model_config = ConfigDict(frozen=True)

    creation_range_index: int = 0
    last_position: DepositCreationPosition | None = None


class RescanPass(BaseModel):
    """One pass of a rescan tier over its deposits: its creation ranges one after another, each in (created_at, id)
    order (2026-10-13). The recent tier's is held in memory only, so a restarted watcher starts it afresh; the old
    tier's is also kept in Redis (``OldRescanTierState``), so a restart resumes it."""

    tier: RescanTier
    # Unix seconds, wall clock: the old tier's pass outlives the process.
    started_at_timestamp: float
    deposits_per_tick: int
    # Each holds the pass's cutoff, fixed when it started.
    creation_ranges: list[DepositCreationRange]
    position: RescanPassPosition = RescanPassPosition()
    deposits_read: int = 0
    deposits_with_money: int = 0
    ticks_taken: int = 0


class OldRescanTierState(BaseModel):
    """The old rescan tier's running pass and when its last pass started, kept in Redis (``store.py``) after every
    applied slice, so a deploy resumes the pass instead of rereading the oldest deposits first, and the tier's interval
    holds across restarts (2026-10-13). A copy only: without it the next pass starts from the first deposit."""

    model_config = ConfigDict(frozen=True)

    chain_key: str
    running_pass: RescanPass | None
    # Unix seconds, wall clock.
    last_pass_started_at_timestamp: float


class WatcherHeartbeat(BaseModel):
    """Written to Redis after every completed tick of one chain (``store.py``); the watcher container's healthcheck
    and ``reconcile`` read it (2026-10-12)."""

    model_config = ConfigDict(frozen=True)

    chain_key: str
    # Unix seconds, wall clock: the healthcheck runs in another process.
    completed_at_timestamp: float


class ReconciliationFinding(BaseModel):
    """One thing that needs the operator. ``problem_key`` stays the same while the problem lasts, so it alerts once
    and then only at the reminder cadence (operator_alert_recorder.py); ``alert_kind`` is set on the findings the watcher tells the operator account about
    (``operator.alert``), None on those only ``reconcile`` shows."""

    model_config = ConfigDict(frozen=True)

    problem_key: str
    alert_kind: str | None
    invoice_id: str | None
    # One plain sentence with no address: it is also the alert's text on /activity.
    message: str
    # The row ``reconcile`` prints, in column order.
    columns: dict[str, str]


class ReconciliationCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check_name: str
    # One line on what was read, printed whether or not anything was found.
    summary: str
    findings: list[ReconciliationFinding]
    # Something the operator should know that needs no action and fails nothing (``reconcile`` prints it as the
    # verdict; the watcher's alerts ignore it): a Monero chain idle because no sidecar is configured.
    warning: str | None = None


class OperatorAlertRecording(BaseModel):
    """What ``OperatorAlertRecorder.record_new_findings`` did: the problems it alerted now, opened or reminded (logged,
    and recorded when the operator account exists); the rest were alerted before and no reminder is due.
    ``operator_account_found`` is None when this process recorded them all lately, so no account was looked up."""

    model_config = ConfigDict(frozen=True)

    alerted_problem_keys: list[str]
    operator_account_found: bool | None


class StaticDepositScanPass(BaseModel):
    """One pass of a static deposit scan tier over its addresses in (created_at, id) order (PLAN.md section 17), held
    in memory: a restarted watcher starts a fresh one."""

    # The hot tier (made or paid since ``hot_since``) or the cold one (every other address); fixed when it starts.
    is_hot_tier: bool
    hot_since: datetime
    # Unix seconds.
    started_at_timestamp: float
    addresses_per_tick: int
    # The last address read; None before the first slice.
    last_scanned: ScannedStaticDeposit | None = None
    addresses_read: int = 0
    addresses_with_new_money: int = 0
    ticks_taken: int = 0
