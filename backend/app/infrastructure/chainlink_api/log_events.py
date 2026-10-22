"""Typed log records of Chainlink feed reads."""

import logging

from app.shared.logging import LogEvent


class ChainlinkEndpointStale(LogEvent):
    """An endpoint's ``latest`` answers are too old, so the read moves to the next endpoint. The endpoint is named by
    its position in the chain's list, never its URL (a configured URL may carry a key)."""

    level = logging.WARNING
    message = "A price endpoint served a Chainlink answer older than allowed; trying the next endpoint"
    operation = "price_read"
    endpoint_index: int
    age_seconds: int


class ChainlinkSequencerRefused(LogEvent):
    """An endpoint's sequencer uptime feed says prices are not to be used (down, back inside the grace period, or
    unreadable), so the read moves to the next endpoint. The endpoint is named by its position, never its URL."""

    level = logging.WARNING
    message = "The rollup's sequencer uptime feed refuses prices; trying the next endpoint"
    operation = "price_read"
    endpoint_index: int
    reason: str
