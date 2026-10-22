"""Typed log records of sidecar calls: the route and the failure kind, never a body, an address or a key."""

import logging

from app.shared.logging import LogEvent


class MoneroSidecarRequestFailed(LogEvent):
    level = logging.WARNING
    message = "A Monero sidecar request failed"
    operation = "monero_sidecar"
    route: str
    failure_kind: str
    http_status: int | None
