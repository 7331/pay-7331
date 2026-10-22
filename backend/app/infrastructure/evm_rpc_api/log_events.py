"""Typed log records of the EVM JSON-RPC client."""

import logging

from app.shared.logging import LogEvent


class EvmRpcEndpointRateLimited(LogEvent):
    """An endpoint rate limited us and is set aside. Named by its host, never its URL (a configured URL may carry a
    key in its path)."""

    level = logging.WARNING
    message = "An RPC endpoint rate limited us; it gets no request until its pause ends"
    operation = "rpc_rate_limit"
    endpoint_host: str
    pause_seconds: int
