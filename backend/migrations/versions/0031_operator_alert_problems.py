"""Operator alerts: once per problem, with Discord reminders at the operator's cadence.

`operator_alert_problems`: one row per open problem (`problem_key`), when it opened (`created_at`), was last seen and
last alerted,
so an alert goes out when a problem opens and again only at the cadence the operator set
(`account_discord_webhooks.operator_alert_repeat_hours`, 1 by default, 0 for never). Seeded from the last hour's
`operator.alert` events, so the problems open at deploy do not alert again at once. Both are new, so the running
release reads neither and the step is additive.

Revision ID: 0031
Revises: 0030
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_alert_problems",
        sa.Column("problem_key", sa.Text(), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_alerted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "account_discord_webhooks",
        sa.Column("operator_alert_repeat_hours", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
    )
    op.execute(
        """
        INSERT INTO operator_alert_problems (problem_key, created_at, last_seen_at, last_alerted_at)
        SELECT data->>'problem_key', min(created_at), max(created_at), max(created_at)
        FROM account_events
        WHERE event_type = 'operator.alert' AND created_at >= now() - interval '1 hour'
            AND data->>'problem_key' IS NOT NULL
        GROUP BY data->>'problem_key'
        """
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
