"""Typed log records of the Redis client's lifecycle."""

from app.shared.logging import LogEvent


class RedisInitialized(LogEvent):
    message = "Redis initialized"
    operation = "redis_initialize"


class RedisClosed(LogEvent):
    message = "Redis closed"
    operation = "redis_close"
