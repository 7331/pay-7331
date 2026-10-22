"""Account API tokens and the account webhook (PLAN.md sections 7 and 8.3).

- `account_api_tokens`: an account's bearer tokens for its own server (name, SHA-256 of the token, last use); the
  token itself is shown once and never stored. Deleted with the account; revoking deletes the row.
- `account_webhook_endpoints`: at most one https URL per account and its Standard Webhooks secret, AES-GCM encrypted
  (payments/account_webhooks/secret_encryption.py), since the API signs with it.
- `account_webhook_deliveries`: one row per message sent to an account's webhook (an account event, or a test), with
  its exact body, so every retry and resend carries the same `webhook-id` and body.
- `account_events.webhook_claimed_at`: set when the API's webhook loop took the row. Existing rows are marked taken
  now, so turning this on replays nothing; the partial index holds only the few rows not yet taken.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def audit_timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "account_api_tokens",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *audit_timestamp_columns(),
    )
    op.create_index("ix_account_api_tokens_account_id", "account_api_tokens", ["account_id"])
    op.create_table(
        "account_webhook_endpoints",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        *audit_timestamp_columns(),
    )
    op.create_table(
        "account_webhook_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "account_event_id",
            sa.BigInteger(),
            sa.ForeignKey("account_events.id", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
        sa.Column("webhook_message_id", sa.String(64), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("scheduled_failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_response_status", sa.Integer(), nullable=True),
        sa.Column("last_failure_reason", sa.String(64), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        *audit_timestamp_columns(),
    )
    op.create_index("ix_account_webhook_deliveries_account_id_id", "account_webhook_deliveries", ["account_id", "id"])
    op.create_index(
        "ix_account_webhook_deliveries_pending_next_attempt_at",
        "account_webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.add_column("account_events", sa.Column("webhook_claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE account_events SET webhook_claimed_at = now()")
    op.create_index(
        "ix_account_events_webhook_unclaimed",
        "account_events",
        ["id"],
        postgresql_where=sa.text("webhook_claimed_at IS NULL"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
