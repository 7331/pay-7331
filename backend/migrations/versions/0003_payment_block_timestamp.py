"""Add invoice_payments.block_timestamp: a credit is valued at the quote live at its payment's block time.

Nullable: rows made before this revision have none, and their credit falls back to the finalized head's timestamp.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoice_payments", sa.Column("block_timestamp", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
