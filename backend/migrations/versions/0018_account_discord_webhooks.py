"""Account Discord webhooks (PLAN.md section 8.4).

- `account_discord_webhooks`: at most one Discord channel webhook per account, its id in the clear (unique: Discord
  limits per webhook, so two accounts sharing one could not be kept to its limit) and its token AES-GCM sealed
  (payments/account_webhooks/secret_encryption.py), when Discord said it was deleted, `next_discord_send_at` (the
  account's Discord sends wait until then: a spent rate-limit bucket, a 429 or a failed message's retry) and
  `discord_send_lease_ends_at` (a delivery pass has the account's messages out until then), so one pass at a time
  sends an account's messages, in order.
- `account_webhook_deliveries.receiver_kind`: `standard_webhook` (every row so far) or `discord`. One account event is
  now queued once per kind, so the unique key on `account_event_id` alone becomes one on (`account_event_id`,
  `receiver_kind`). Both steps are additive for the running code: the old key's guarantee is kept for the old rows.
- `account_webhook_deliveries.queued_at`: when it was queued or last resent; a Discord 429 past the retry schedule's
  span from then fails it. Existing rows take the migration's time.
- Indexes for the two claims: (`receiver_kind`, `next_attempt_at`) of pending rows replaces 0015's (`next_attempt_at`)
  one, which the planner kept using while it existed, so the webhook claim stepped over held Discord rows; and
  (`account_id`, `receiver_kind`, `next_attempt_at`) of pending rows for the Discord claim's per-account reads.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_discord_webhooks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("discord_webhook_id", sa.String(32), nullable=False),
        sa.Column("encrypted_discord_webhook_token", sa.LargeBinary(), nullable=False),
        sa.Column("deleted_in_discord_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_discord_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discord_send_lease_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "account_webhook_deliveries",
        sa.Column("receiver_kind", sa.String(16), nullable=False, server_default=sa.text("'standard_webhook'")),
    )
    op.add_column(
        "account_webhook_deliveries",
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.drop_constraint("account_webhook_deliveries_account_event_id_key", "account_webhook_deliveries", type_="unique")
    op.create_unique_constraint(
        "uq_account_webhook_deliveries_account_event_id_receiver_kind",
        "account_webhook_deliveries",
        ["account_event_id", "receiver_kind"],
    )
    op.create_index(
        "uq_account_discord_webhooks_discord_webhook_id",
        "account_discord_webhooks",
        ["discord_webhook_id"],
        unique=True,
    )
    op.drop_index("ix_account_webhook_deliveries_pending_next_attempt_at", table_name="account_webhook_deliveries")
    op.create_index(
        "ix_account_webhook_deliveries_pending_kind_next_attempt_at",
        "account_webhook_deliveries",
        ["receiver_kind", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_account_webhook_deliveries_pending_account_kind_next_attempt",
        "account_webhook_deliveries",
        ["account_id", "receiver_kind", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
