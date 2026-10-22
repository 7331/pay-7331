"""Typed log records of the chain watcher."""

import logging

from app.shared.logging import LogEvent


class MoneroScannerUnreadable(LogEvent):
    level = logging.WARNING
    message = "The Monero scanner sidecar did not answer its health read; this tick reads and credits nothing"
    operation = "monero_chain_watcher_tick"
    chain_key: str


class MoneroScannerStalled(LogEvent):
    level = logging.WARNING
    message = (
        "The Monero scanner sidecar answered, but its scan loop has not completed a tick within 3 poll intervals; this"
        " tick reads and credits nothing"
    )
    operation = "monero_chain_watcher_tick"
    chain_key: str
    seconds_since_tick: int


class MoneroScannerBehind(LogEvent):
    level = logging.WARNING
    message = "The Monero scanner sidecar's scan trails its tip; payments in the unscanned blocks are not seen yet"
    operation = "monero_chain_watcher_tick"
    chain_key: str
    scanned_height: int | None
    tip_height: int


class MoneroWalletsUnusable(LogEvent):
    level = logging.ERROR
    message = (
        "The Monero scanner sidecar cannot open or match the view key of watched wallets and holds its scan until they"
        " are fixed (its log names them: monero_wallet_unusable); no invoice of the chain expires and no heartbeat is"
        " written meanwhile"
    )
    operation = "monero_chain_watcher_tick"
    chain_key: str
    unusable_wallet_count: int


class WatcherCatchingUp(LogEvent):
    message = "The watcher is behind the chain; reading more logs before applying balances"
    operation = "chain_watcher_tick"
    chain_key: str
    stream_name: str
    last_block_number: int
    target_block_number: int


class LogRangeNarrowed(LogEvent):
    message = "The endpoint refused an eth_getLogs range; reading half of it while ticks stay on this endpoint"
    operation = "chain_watcher_tick"
    chain_key: str
    stream_name: str
    from_block_number: int
    refused_range_blocks: int
    next_range_blocks: int


class TransferLogsStoppedAtFinalized(LogEvent):
    message = "An eth_getLogs toBlock past the endpoint's head was refused; Transfer reads stop at finalized this tick"
    operation = "chain_watcher_tick"
    chain_key: str
    refused_to_block_number: int
    finalized_block_number: int


class FinalizedHeadWentBack(LogEvent):
    level = logging.WARNING
    message = "The endpoint's finalized block is below the last one applied; tick skipped"
    operation = "chain_watcher_tick"
    chain_key: str
    finalized_block_number: int
    last_applied_block_number: int


class DepositsRescanned(LogEvent):
    message = "Every deposit of a rescan tier rescanned in every asset"
    operation = "chain_watcher_rescan"
    chain_key: str
    # `recent` or `old` (RescanTier).
    rescan_tier: str
    deposit_count: int
    deposits_with_money: int
    tick_count: int
    pass_seconds: float


class RescanSlowerThanInterval(LogEvent):
    level = logging.WARNING
    message = (
        "A rescan tier's pass took longer than its interval; raise RESCAN_MAXIMUM_BALANCE_CALLS_PER_TICK or shorten"
        " recent_deposit_window_seconds"
    )
    operation = "chain_watcher_rescan"
    chain_key: str
    # `recent` or `old` (RescanTier).
    rescan_tier: str
    pass_seconds: float
    # The tier's interval: `rescan_interval_seconds` or `old_deposit_rescan_interval_seconds`.
    tier_interval_seconds: float


class StaticDepositsScanned(LogEvent):
    message = "Every static deposit address of a scan tier scanned in every asset"
    operation = "chain_watcher_static_deposit_scan"
    chain_key: str
    # `hot` or `cold` (PLAN.md section 17).
    scan_tier: str
    address_count: int
    addresses_with_new_money: int
    tick_count: int
    pass_seconds: float


class StaticDepositScanSlowerThanInterval(LogEvent):
    level = logging.WARNING
    message = (
        "A static deposit scan tier's pass took longer than its interval; raise "
        "STATIC_DEPOSIT_MAXIMUM_BALANCE_CALLS_PER_TICK or cap PAY_MAXIMUM_STATIC_DEPOSIT_ADDRESSES_PER_ACCOUNT lower"
    )
    operation = "chain_watcher_static_deposit_scan"
    chain_key: str
    # `hot` or `cold`.
    scan_tier: str
    pass_seconds: float
    interval_seconds: float


class StaticDepositAttributionSkipped(LogEvent):
    level = logging.WARNING
    message = "Transfer logs into static deposit addresses unreadable; their money is recorded with no sender"
    operation = "chain_watcher_static_deposit_scan"
    chain_key: str
    from_block_number: int
    to_block_number: int
    error_type: str


class OldRescanTierStateUnavailable(LogEvent):
    level = logging.WARNING
    message = (
        "The old rescan tier's pass could not be read from or written to Redis; it goes on in memory, and a restart"
        " now would start it afresh"
    )
    operation = "chain_watcher_rescan"
    chain_key: str
    # `read` or `write`.
    redis_operation: str


class WatcherHeartbeatNotWritten(LogEvent):
    level = logging.WARNING
    message = "The watcher's heartbeat could not be written to Redis; the container healthcheck will see it stale"
    operation = "chain_watcher_heartbeat"
    chain_key: str


class OperatorAlertRaised(LogEvent):
    level = logging.WARNING
    message = "Operator alert: something needs the operator (kept on the operator account's /activity)"
    operation = "operator_alert"
    # None for an alert about no chain (the host's backups).
    chain_key: str | None
    alert_kind: str
    problem_key: str
    # False when PAY_OPERATOR_ACCOUNT_IDS is empty or its first id names no account: this line is all there is.
    recorded_on_account: bool


class OperatorAlertCheckFailed(LogEvent):
    level = logging.ERROR
    message = "The operator alert checks failed; the next check retries"
    operation = "operator_alert"
    chain_key: str


class OperatorKeyMaintenanceFailed(LogEvent):
    level = logging.ERROR
    message = "The operator key look (leak check, top-ups, returns) failed; the next one retries"
    operation = "operator_key"
    chain_key: str


class WatcherLivenessUnreadable(LogEvent):
    level = logging.WARNING
    message = "The API could not read the watcher's heartbeat from Redis; this liveness check is skipped"
    operation = "watcher_liveness"
    chain_key: str


class IpApiKeyRejectionUnreadable(LogEvent):
    level = logging.WARNING
    message = "The API could not read the ip-api key rejection marker from Redis; this check is skipped"
    operation = "ip_api_key_alert"


class WatcherLockHeldElsewhere(LogEvent):
    level = logging.WARNING
    message = "Another watcher holds the advisory lock; this one idles"
    operation = "chain_watcher_lock"
