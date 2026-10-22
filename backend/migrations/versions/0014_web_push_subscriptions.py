"""Web Push: the browsers an account turned notifications on for, and which events went out (2026-10-12; PLAN.md
section 8.2).

- `web_push_subscriptions`: one row per browser subscription (`PushSubscription.endpoint`, unique, and its two keys),
  deleted with its account.
- `account_events.web_push_claimed_at`: set when the API's delivery took the row, so each is pushed at most once.
  Null for every existing row; only rows of the last 10 minutes are ever taken, so nothing old is pushed.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.String(2048), nullable=False, unique=True),
        sa.Column("p256dh_key", sa.String(100), nullable=False),
        sa.Column("auth_secret", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_web_push_subscriptions_account_id", "web_push_subscriptions", ["account_id"])
    op.add_column("account_events", sa.Column("web_push_claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
