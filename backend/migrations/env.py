"""Alembic environment: a throwaway async engine from DATABASE_URL."""

import asyncio
import os

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.postgres.base import Base
import app.payments.account_events.models
import app.payments.account_webhooks.models
import app.payments.accounts.models
import app.payments.chain_watcher.models
import app.payments.invoice_schedules.models
import app.payments.invoices.models
import app.payments.monero_wallets.models
import app.payments.payment_links.models
import app.payments.price_quotes.models
import app.payments.settlements.models
import app.payments.static_deposit_addresses.models  # noqa: F401  # registers every table on Base.metadata

target_metadata = Base.metadata


def run_migrations_on_connection(connection: Connection) -> None:
    # One transaction per migration: a migration's ``autocommit_block()`` (0016's ALTER TYPE ... ADD VALUE)
    # "unconditionally" commits the transaction before it, which with one transaction for the whole run would commit
    # every earlier migration of the run halfway; Alembic recommends this setting for that block
    # (https://alembic.sqlalchemy.org/en/latest/api/runtime.html#alembic.runtime.migration.MigrationContext.autocommit_block,
    # 2026-10-13).
    context.configure(connection=connection, target_metadata=target_metadata, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations_on_connection)
    await engine.dispose()


asyncio.run(run_async_migrations())
