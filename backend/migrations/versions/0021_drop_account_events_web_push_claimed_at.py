"""Drop `account_events.web_push_claimed_at`, the contract step after Web Push's removal (2026-10-13; 0017 dropped
the subscriptions and left the column, since a column change is expand then contract). The running
release no longer maps it, so the drop is safe while it serves. Postgres drops any index on the column with it
(https://www.postgresql.org/docs/current/sql-altertable.html, DROP COLUMN).

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("account_events", "web_push_claimed_at")


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
