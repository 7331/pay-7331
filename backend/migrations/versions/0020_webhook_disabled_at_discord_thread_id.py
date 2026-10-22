"""A webhook receiver's 410 Gone, and Discord webhooks of forum and media channels (2026-10-13; PLAN.md sections 8.3
and 8.4).

- `account_webhook_endpoints.disabled_at`: when the receiver answered 410 Gone. Standard Webhooks: "Sender should
  disable the webhook endpoint, and stop sending it messages"
  (https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md). Setting the URL again
  clears it.
- `account_discord_webhooks.discord_thread_id`: the `?thread_id=` of the pasted URL. Execute Webhook on a forum or
  media channel needs `thread_id` or `thread_name`
  (https://github.com/discord/discord-api-docs/blob/main/developers/resources/webhook.mdx).

Both nullable and unread by the running release, so the step is additive.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("account_webhook_endpoints", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("account_discord_webhooks", sa.Column("discord_thread_id", sa.String(length=32), nullable=True))


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
