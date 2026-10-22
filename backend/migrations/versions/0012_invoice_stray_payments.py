"""Money in a coin the invoice did not ask for, seen while the invoice is live (INC-001, 2026-10-11; PLAN.md
sections 4 and 5).

- `invoice_stray_payments`: one row per transfer (token Transfer log, native transaction, or a native rise no
  transaction explains), in that asset's base units; never credited to the invoice.
- `invoices.latest_native_balance_base_units`: a token invoice's native balance at `latest`, so a native rise is
  matched to its transaction like a native invoice's payment. 0 for every existing invoice.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("latest_native_balance_base_units", sa.Numeric(78, 0), server_default=sa.text("0"), nullable=False),
    )
    op.create_table(
        "invoice_stray_payments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("invoice_id", sa.String(26), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("asset_symbol", sa.String(16), nullable=False),
        sa.Column("transaction_hash", sa.String(66), nullable=True),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("transaction_index", sa.Integer(), nullable=False),
        sa.Column("sender", sa.String(42), nullable=True),
        sa.Column("sender_has_code", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("amount_base_units", sa.Numeric(78, 0), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("block_hash", sa.String(66), nullable=False),
        sa.Column("block_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("final", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("invoice_id", "asset_symbol", "transaction_hash", "log_index"),
    )
    op.create_index("ix_invoice_stray_payments_invoice_id", "invoice_stray_payments", ["invoice_id"])
    op.create_index(
        "ix_invoice_stray_payments_unfinal",
        "invoice_stray_payments",
        ["block_number"],
        postgresql_where=sa.text("final IS false"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
