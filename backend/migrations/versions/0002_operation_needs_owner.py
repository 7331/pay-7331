"""Add needs_owner to operation_status: an unproven end that keeps a refund's amount reserved.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE operation_status ADD VALUE IF NOT EXISTS 'needs_owner'")


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
