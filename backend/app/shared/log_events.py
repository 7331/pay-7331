"""Typed log records of shared's request plumbing: the error handlers. A domain's records live in its own
``log_events.py``; the processes' records in ``app/log_events.py``."""

import logging

from app.shared.logging import LogEvent


class UnhandledError(LogEvent):
    level = logging.ERROR
    message = "Unhandled error"
    operation = "request_error"
    route: str


class RedisUnavailable(LogEvent):
    level = logging.WARNING
    message = "Redis unavailable during a request"
    operation = "request_error"
    route: str
