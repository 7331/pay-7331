"""Structured JSON logging via structlog, one configured root (no separate
logging package: setup and the typed log events both live here).

Rendering happens in a structlog ``ProcessorFormatter`` on the stdlib stream handler, so app records and foreign
records (uvicorn, SQLAlchemy, httpx) render through the same pipeline. stderr is the only sink: Docker collects
it. The schema is ``level`` / ``logger`` / ``timestamp`` / ``message`` / ``operation`` plus the event's fields,
the bound context and ``correlation_id``.

Every record is a typed ``LogEvent`` from the owning package's ``log_events.py``: a fixed message and operation,
values in fields. Per-scope context (a chain, an invoice id, an operation id) is bound once with
``bound_log_context`` and merged into every line in that scope. The request ``correlation_id`` is read from
asgi-correlation-id's contextvar, which ``ServerMintedCorrelationIdMiddleware`` sets per request.
"""

from collections.abc import Generator
from contextlib import contextmanager
import logging
import re
import sys
from typing import ClassVar

from asgi_correlation_id import correlation_id
from pydantic import BaseModel, ConfigDict
import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

__all__ = [
    "LogContext",
    "LogEvent",
    "bound_log_context",
    "configure_logging",
    "emit",
    "exception",
    "get_logger",
]


class LogEvent(BaseModel):
    """A validated application record; subclasses fix its message and operation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    level: ClassVar[int] = logging.INFO
    message: ClassVar[str]
    operation: ClassVar[str]


def emit(logger: structlog.stdlib.BoundLogger, *, event: LogEvent, exception: bool | BaseException = False) -> None:
    """Write one typed record, with an exception's traceback when given (also outside its except block)."""
    logger.log(event.level, event.message, operation=event.operation, **event.model_dump(), exc_info=exception)


def exception(logger: structlog.stdlib.BoundLogger, *, event: LogEvent) -> None:
    """Write one typed record with the traceback of the exception being handled. The name ``exception`` is the
    one ruff's BLE001 recognises as logging a broad catch."""
    emit(logger, event=event, exception=True)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A lazy logger on the process's structlog configuration."""
    return structlog.stdlib.get_logger(name)


class LogContext(BaseModel):
    """Identifiers admitted to a scope's log context; never submitted payloads or secrets."""

    model_config = ConfigDict(frozen=True)

    chain_key: str | None = None
    invoice_id: str | None = None
    operation_id: int | None = None


@contextmanager
def bound_log_context(context: LogContext) -> Generator[None]:
    """Bind the supplied fields for the ``with`` block; every line written inside it carries them."""
    with structlog.contextvars.bound_contextvars(**context.model_dump(exclude_unset=True)):
        yield


# Loggers uvicorn's own dictConfig gives a plain-text handler and ``propagate: False``; their records go to the
# root handler instead.
UVICORN_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Library loggers pinned to WARNING. ``uvicorn.access`` logs the request target verbatim;
# ``app.request_log_middleware`` writes the one line per request instead.
NOISY_LOGGERS: tuple[str, ...] = ("sqlalchemy.engine", "watchfiles", "httpx", "httpcore", "asyncio", "uvicorn.access")


def add_correlation_id(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Attach the request's correlation id; ``-`` outside a request (the watcher loops)."""
    event_dict["correlation_id"] = correlation_id.get() or "-"
    return event_dict


def drop_color_message(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Drop uvicorn's ``color_message`` extra: the same text as the message, with terminal escapes."""
    event_dict.pop("color_message", None)
    return event_dict


def uppercase_level(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    level = event_dict.get("level")
    if level:
        event_dict["level"] = level.upper()
    return event_dict


DATABASE_DETAIL = re.compile(r"^DETAIL: .*$", re.MULTILINE)
# A quoted literal anywhere on a line: an exception message quoting its input.
QUOTED_VALUE = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")
# The traceback's own frame lines, whose quoted string is a source path, not a value.
FRAME_LINE = re.compile(r'^\s*File "')


def redact_line(line: str) -> str:
    """One traceback line with its quoted literals replaced; a frame line is left alone."""
    if FRAME_LINE.match(line):
        return line
    return QUOTED_VALUE.sub("'[redacted]'", line)


def redact_traceback_values(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Take quoted values and Postgres DETAIL lines out of a rendered traceback, keeping its shape."""
    rendered = event_dict.get("exception")
    if isinstance(rendered, str):
        rendered = DATABASE_DETAIL.sub("DETAIL:  [redacted]", rendered)
        event_dict["exception"] = "\n".join(redact_line(line) for line in rendered.split("\n"))
    return event_dict


# Processors shared by native structlog events and foreign (stdlib) records.
SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    uppercase_level,
    add_correlation_id,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    structlog.processors.StackInfoRenderer(),
]


def configure_logging(log_level: str) -> None:
    """JSON on stderr, or a readable console renderer when stderr is a terminal."""
    interactive = sys.stderr.isatty()
    processors = [*SHARED_PROCESSORS]
    if not interactive:
        processors.extend([structlog.processors.format_exc_info, redact_traceback_values])
    renderer = (
        structlog.dev.ConsoleRenderer(event_key="message") if interactive else structlog.processors.JSONRenderer()
    )
    # Refill the configured list in place: a logger cached on first use keeps a reference to it.
    configured = structlog.get_config()["processors"]
    configured[:] = [*processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter]
    structlog.configure(
        processors=configured,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        # Foreign records run the shared chain plus ExtraAdder, so a library's ``extra={...}`` fields survive.
        foreign_pre_chain=[*processors, structlog.stdlib.ExtraAdder(), drop_color_message],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.EventRenamer("message"),
            renderer,
        ],
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.getLevelNamesMapping()[log_level])
    root.handlers = [stream_handler]
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
