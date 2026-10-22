"""Static deposit addresses: one permanent deposit per (account, chain, customer reference)
(PLAN.md section 17).

- `static_deposit_addresses`: the address, its salt, destination and coins, `sweep_blocked`, the tokens its sweeps
  leave out (`skipped_sweep_token_symbols`, set by `sweep-static-deposit-address --skip-token`), the finalized block its
  last applied scan read (`last_scanned_block_number`) and when it last received money (`last_received_at`, the
  scan's hot tier). Unique per (account, chain, customer reference);
  `ix_static_deposit_addresses_account_id_reference_pattern` serves the prefix list (the collation is not C,
  https://www.postgresql.org/docs/18/indexes-opclass.html); `ix_static_deposit_addresses_chain_key_created_at_id`
  pages the watcher's scan, `ix_static_deposit_addresses_chain_key_last_received_at` finds its hot tier and the
  partial `ix_static_deposit_addresses_chain_key_sweep_blocked` the watcher's minute check of blocked sweeps.
- `static_deposit_payments`: each rise in what an address received, credited or not, with its USD value.
- `operation_kind` gains `static_deposit_sweep`, and `outgoing_operations.static_deposit_address_id` names its
  address (no invoice).

All new: the running release reads none of it, so the step is additive.

Revision ID: 0033
Revises: 0032
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE operation_kind ADD VALUE IF NOT EXISTS 'static_deposit_sweep'")
    op.create_table(
        "static_deposit_addresses",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("chain_key", sa.String(32), nullable=False),
        sa.Column("customer_reference", sa.String(128), nullable=False),
        sa.Column("assets", postgresql.ARRAY(sa.String(16)), nullable=False),
        sa.Column("deposit_salt", sa.LargeBinary(32), nullable=False),
        sa.Column("deposit_address", sa.String(42), nullable=False, unique=True),
        sa.Column("destination_address", sa.String(42), nullable=False),
        sa.Column("sweep_blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "skipped_sweep_token_symbols",
            postgresql.ARRAY(sa.String(16)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("last_scanned_block_number", sa.BigInteger(), nullable=True),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "chain_key",
            "customer_reference",
            name="uq_static_deposit_addresses_account_chain_reference",
        ),
    )
    op.create_index(
        "ix_static_deposit_addresses_account_id_reference_pattern",
        "static_deposit_addresses",
        ["account_id", "customer_reference"],
        postgresql_ops={"customer_reference": "varchar_pattern_ops"},
    )
    op.create_index(
        "ix_static_deposit_addresses_account_id_created_at_id",
        "static_deposit_addresses",
        ["account_id", "created_at", "id"],
    )
    op.create_index(
        "ix_static_deposit_addresses_chain_key_created_at_id",
        "static_deposit_addresses",
        ["chain_key", "created_at", "id"],
    )
    op.create_index(
        "ix_static_deposit_addresses_chain_key_last_received_at",
        "static_deposit_addresses",
        ["chain_key", "last_received_at"],
    )
    op.create_index(
        "ix_static_deposit_addresses_chain_key_sweep_blocked",
        "static_deposit_addresses",
        ["chain_key"],
        postgresql_where=sa.text("sweep_blocked"),
    )
    op.create_table(
        "static_deposit_payments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "static_deposit_address_id", sa.String(26), sa.ForeignKey("static_deposit_addresses.id"), nullable=False
        ),
        sa.Column("asset_symbol", sa.String(16), nullable=False),
        sa.Column("amount_base_units", sa.Numeric(78, 0), nullable=False),
        sa.Column("asset_price_usd", sa.Numeric(38, 18), nullable=True),
        sa.Column("amount_usd", sa.Numeric(20, 2), nullable=True),
        sa.Column("credited", sa.Boolean(), nullable=False),
        sa.Column("uncredited_reason", sa.String(32), nullable=True),
        sa.Column("transaction_hash", sa.String(66), nullable=True),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("sender", sa.String(42), nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "static_deposit_address_id",
            "transaction_hash",
            "log_index",
            name="uq_static_deposit_payments_address_transaction_log",
        ),
        sa.CheckConstraint(
            "credited = (uncredited_reason IS NULL)", name="ck_static_deposit_payments_credited_or_reason"
        ),
    )
    op.create_index(
        "ix_static_deposit_payments_static_deposit_address_id_id",
        "static_deposit_payments",
        ["static_deposit_address_id", "id"],
    )
    op.add_column("outgoing_operations", sa.Column("static_deposit_address_id", sa.String(26), nullable=True))
    op.create_index(
        "ix_outgoing_operations_static_deposit_address_id",
        "outgoing_operations",
        ["static_deposit_address_id"],
        postgresql_where=sa.text("static_deposit_address_id IS NOT NULL"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
