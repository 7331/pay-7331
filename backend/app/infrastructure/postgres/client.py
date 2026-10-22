"""PostgreSQL async engine and session factory over psycopg 3, plus the Alembic head check,
an advisory lock for the single watcher, and a side transaction engine for the API.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure.postgres.log_events import DatabaseClosed
from app.shared import logging as log

logger = log.get_logger(__name__)

# backend/alembic.ini, next to the app package in the repository and in the image alike.
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"

DATABASE_CONNECT_TIMEOUT_SECONDS = 10
DATABASE_STATEMENT_TIMEOUT_SECONDS = 30
DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS = 60
DATABASE_LOCK_TIMEOUT_SECONDS = 10


# The key under which a session made by ``Database.session_factory`` finds the side transaction engine.
SIDE_TRANSACTION_ENGINE_SESSION_INFO_KEY = "side_transaction_engine"


def create_configured_async_engine(
    dsn: str, *, application_name: str, pool_size: int, maximum_overflow: int
) -> AsyncEngine:
    return create_async_engine(
        dsn,
        pool_size=pool_size,
        max_overflow=maximum_overflow,
        pool_timeout=30,
        pool_use_lifo=True,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "application_name": application_name,
            "options": (
                f"-c statement_timeout={DATABASE_STATEMENT_TIMEOUT_SECONDS * 1000} "
                f"-c idle_in_transaction_session_timeout={DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS * 1000} "
                f"-c lock_timeout={DATABASE_LOCK_TIMEOUT_SECONDS * 1000}"
            ),
            "connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS,
        },
        execution_options={"isolation_level": "READ COMMITTED"},
    )


class Database:
    """One async engine and its session factory, and optionally a tiny side transaction engine (same URL and
    settings, its own pool of ``side_transaction_pool_size``): for a step that must commit on a connection of its own
    while the request's transaction holds a connection and locks, such as a fresh passkey check's consumption. A
    second connection from the request pool could wait on that pool while every request holding it waits on this
    step. Sessions find it in ``session.info`` under ``SIDE_TRANSACTION_ENGINE_SESSION_INFO_KEY``."""

    def __init__(
        self,
        dsn: str,
        *,
        application_name: str,
        pool_size: int,
        maximum_overflow: int,
        side_transaction_pool_size: int | None = None,
    ) -> None:
        self.engine = create_configured_async_engine(
            dsn, application_name=application_name, pool_size=pool_size, maximum_overflow=maximum_overflow
        )
        self.side_transaction_engine = (
            create_configured_async_engine(
                dsn,
                application_name=application_name,
                pool_size=side_transaction_pool_size,
                maximum_overflow=side_transaction_pool_size,
            )
            if side_transaction_pool_size is not None
            else None
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            info={SIDE_TRANSACTION_ENGINE_SESSION_INFO_KEY: self.side_transaction_engine},
        )

    async def assert_at_head(self) -> None:
        """Refuse to serve unless the schema is at this build's Alembic head (the deploy runs the migration)."""
        async with self.engine.connect() as connection:
            revision = await connection.run_sync(
                lambda sync_connection: MigrationContext.configure(sync_connection).get_current_revision()
            )
        head = ScriptDirectory.from_config(AlembicConfig(str(ALEMBIC_INI))).get_current_head()
        if revision != head:
            raise RuntimeError(f"schema is at {revision}, this build needs {head}; run `alembic upgrade head`")

    @asynccontextmanager
    async def hold_advisory_lock(self, lock_key: int) -> AsyncGenerator[bool]:
        """Try a session-level advisory lock on a connection held for the block; yields whether it was taken. The
        lock goes with the connection, so a process that dies releases it."""
        async with self.engine.connect() as connection:
            taken = bool(
                await connection.scalar(text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": lock_key})
            )
            await connection.commit()
            try:
                yield taken
            finally:
                if taken:
                    await connection.execute(text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key})
                    await connection.commit()

    async def close(self) -> None:
        await self.engine.dispose()
        if self.side_transaction_engine is not None:
            await self.side_transaction_engine.dispose()
        log.emit(logger, event=DatabaseClosed())
