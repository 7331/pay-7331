"""Typed log records of the PostgreSQL engine."""

from app.shared.logging import LogEvent


class DatabaseClosed(LogEvent):
    message = "Database engine closed"
    operation = "database_close"
