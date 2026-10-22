"""Sweeps that leave tokens out (2026-10-13; PLAN.md section 5 step 6).

- `outgoing_operations.skipped_token_addresses`: the tokens a hand sweep (`sweep-invoice --skip-token`) never lists,
  so a token whose transfer reverts (a frozen USDC deposit or destination, a paused token) does not hold up the
  deposit's native coin and other tokens. Every existing row and every automatic sweep takes the empty list. Additive:
  the running code does not map it.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outgoing_operations",
        sa.Column(
            "skipped_token_addresses",
            postgresql.ARRAY(sa.String(42)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
