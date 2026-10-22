"""Typed log records of the composition root: the process lifespans, the process loops and the request log."""

import logging

from app.shared.logging import LogEvent


class ApplicationStarted(LogEvent):
    message = "Application started"
    operation = "lifespan_startup"


class ApplicationStopped(LogEvent):
    message = "Application stopped"
    operation = "lifespan_shutdown"


class WatcherProcessStarted(LogEvent):
    message = "Watcher process started"
    operation = "watcher_lifespan_startup"
    chain_keys: list[str]


class ChainNotReadyForWatching(LogEvent):
    level = logging.WARNING
    message = "Chain enabled but not deployed yet; the watcher skips it"
    operation = "watcher_lifespan_startup"
    chain_key: str
    missing_setting: str


class MoneroChainsIdle(LogEvent):
    """Once at watcher start: Monero chains are enabled but ``PAY_MONERO_SIDECAR_URL`` is empty, so no Monero loop
    runs (no tip to read, nothing to credit) and the healthcheck skips them."""

    level = logging.WARNING
    message = "Monero chains enabled with no scanner sidecar configured; the watcher leaves them idle"
    operation = "watcher_lifespan_startup"
    chain_keys: list[str]
    missing_setting: str


class OperatorAccountNotConfigured(LogEvent):
    """Once at watcher start: ``PAY_OPERATOR_ACCOUNT_IDS`` is empty, so operator alerts are log lines only and no
    invoice gets float refunds."""

    level = logging.WARNING
    message = "No operator account configured; operator alerts are only logged and no invoice gets float refunds"
    operation = "watcher_lifespan_startup"
    missing_setting: str


class OperatorAccountNotFound(LogEvent):
    """Once at watcher start for each ``PAY_OPERATOR_ACCOUNT_IDS`` id no account has: that id gets no float refunds,
    and when it is the first, operator alerts are log lines only."""

    level = logging.WARNING
    message = "A configured operator account id names no account"
    operation = "watcher_lifespan_startup"
    account_id: int
    setting: str


class WatcherProcessStopped(LogEvent):
    message = "Watcher process stopped"
    operation = "watcher_lifespan_shutdown"


class ProcessLoopStopped(LogEvent):
    level = logging.ERROR
    message = "A process loop died"
    operation = "process_loop"
    loop_name: str


class ProcessLoopPassFailed(LogEvent):
    level = logging.WARNING
    message = "A process loop pass failed; the loop goes on"
    operation = "process_loop"
    loop_name: str


class RequestHandled(LogEvent):
    message = "Request handled"
    operation = "request"
    method: str
    route: str
    status: int
    duration_ms: float
