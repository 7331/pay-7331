"""The fields the rate limiter emits."""

import logging

from app.shared.logging import LogEvent


class RateLimitDegraded(LogEvent):
    level = logging.WARNING
    message = "Rate limiter could not reach Redis; the budget degraded"
    operation = "rate_limit_allow"
    scope_kind: str
