"""Drop Web Push's subscriptions (2026-10-13: Web Push removed; alerts are /activity's SSE stream, signed
webhooks and Discord; PLAN.md section 8.2).

- Drop `web_push_subscriptions` (migration 0014) and its index: the browsers' push endpoints and keys, which nothing
  reads any more.
- `account_events.web_push_claimed_at` stays for now: a column change is expand then contract.
  This release no longer maps it; a later revision drops it once this one is live.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_web_push_subscriptions_account_id", table_name="web_push_subscriptions")
    op.drop_table("web_push_subscriptions")


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
