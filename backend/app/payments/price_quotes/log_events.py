"""Typed log records of price reads."""

import logging

from app.shared.logging import LogEvent


class PriceFeedStale(LogEvent):
    level = logging.WARNING
    message = "A Chainlink feed answer is older than the chain allows"
    operation = "price_read"
    chain_key: str
    feed_address: str
    age_seconds: int


class PriceFeedUnreadable(LogEvent):
    level = logging.WARNING
    message = "Chainlink feeds could not be read"
    operation = "price_read"
    chain_key: str


class PriceCacheUnavailable(LogEvent):
    level = logging.WARNING
    message = "The price cache in Redis is unavailable; reading the feeds"
    operation = "price_read"
