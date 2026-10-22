"""Add owner_events: every invoice and refund event, streamed to the owner page (PLAN.md section 8.1).

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "owner_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_owner_events_invoice_id", "owner_events", ["invoice_id"])
    op.create_index("ix_owner_events_created_at", "owner_events", ["created_at"])


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
