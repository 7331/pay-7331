"""Add payment_links and invoices.payment_link_id: quantity links, one account's link that up to N payers pay, each
with an invoice of its own (replacing the earlier no-reusable-links rule; payments/payment_links).

Not the payment_links that 0006 dropped: no open amount, no webhook, no screening overrides, and a quantity. The
invoices index is partial (most invoices have no link). The foreign key lives only here: the invoices model keeps no
ORM one, since the invoices domain sits below payment_links. Existing invoices need no backfill.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_links",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("chain_key", sa.String(32), nullable=False),
        sa.Column("assets", postgresql.ARRAY(sa.String(16)), nullable=False),
        sa.Column("fiat_currency", sa.String(3), nullable=False),
        sa.Column("fiat_amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("destination_address", sa.String(42), nullable=False),
        sa.Column("note", sa.String(500), server_default=sa.text("''"), nullable=False),
        sa.Column("return_url", sa.String(2048), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("lifetime_seconds", sa.Integer(), nullable=False),
        sa.Column("accuracy_percent", sa.Numeric(4, 2), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity BETWEEN 1 AND 1000", name="ck_payment_links_quantity"),
    )
    op.create_index("ix_payment_links_account_id_created_at_id", "payment_links", ["account_id", "created_at", "id"])
    op.add_column(
        "invoices",
        sa.Column("payment_link_id", sa.String(26), sa.ForeignKey("payment_links.id"), nullable=True),
    )
    op.create_index(
        "ix_invoices_payment_link_id",
        "invoices",
        ["payment_link_id"],
        postgresql_where=sa.text("payment_link_id IS NOT NULL"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
