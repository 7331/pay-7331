"""Open sign-up with passkey accounts, account-scoped invoices and events, no webhooks
(PLAN.md sections 7, 8.1, 9 and 11).

- Add accounts, account_passkeys, account_sessions and passkey_challenges; drop the owner-only owner_passkeys,
  owner_sessions, owner_passkey_challenges and owner_passkey_enrollment_tokens (production held no passkey and no
  session; its expired challenge and token rows were backed up before this ran).
- invoices: add account_id (null for an invoice made signed out, and for every invoice before this) and
  client_ip_hash (the salted hash the per-IP open-invoice cap counts); order_id becomes unique per account; drop
  webhook_url.
- Rename owner_events to account_events and add account_id: events of an invoice without an account are kept and
  streamed to nobody.
- Drop webhook_deliveries and its enum (production's rows were backed up before this ran).

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def audit_timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def create_account_tables() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("username", sa.String(length=20), nullable=False),
        sa.Column("payout_address", sa.String(length=42), nullable=False),
        sa.Column("passkey_user_handle", sa.LargeBinary(length=32), nullable=False),
        *audit_timestamp_columns(),
        sa.UniqueConstraint("username"),
        sa.UniqueConstraint("passkey_user_handle"),
        sa.CheckConstraint("username ~ '^[a-z0-9_]{3,20}$'", name="ck_accounts_username_format"),
    )
    op.create_table(
        "account_passkeys",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("transports", postgresql.ARRAY(sa.String(length=16)), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *audit_timestamp_columns(),
        sa.UniqueConstraint("credential_id"),
    )
    op.create_index("ix_account_passkeys_account_id", "account_passkeys", ["account_id"])
    op.create_table(
        "account_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "account_passkey_id",
            sa.BigInteger(),
            sa.ForeignKey("account_passkeys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *audit_timestamp_columns(),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_account_sessions_account_passkey_id", "account_sessions", ["account_passkey_id"])
    op.create_index("ix_account_sessions_expires_at", "account_sessions", ["expires_at"])
    op.create_table(
        "passkey_challenges",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("challenge_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("sign_up_username", sa.String(length=20), nullable=True),
        sa.Column("sign_up_payout_address", sa.String(length=42), nullable=True),
        sa.Column("sign_up_user_handle", sa.LargeBinary(length=32), nullable=True),
        *audit_timestamp_columns(),
        sa.UniqueConstraint("challenge_hash"),
    )
    op.create_index("ix_passkey_challenges_expires_at", "passkey_challenges", ["expires_at"])


def upgrade() -> None:
    create_account_tables()
    op.drop_table("owner_sessions")
    op.drop_table("owner_passkeys")
    op.drop_table("owner_passkey_challenges")
    op.drop_table("owner_passkey_enrollment_tokens")

    op.add_column(
        "invoices",
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("invoices", sa.Column("client_ip_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_invoices_account_id_created_at_id", "invoices", ["account_id", "created_at", "id"])
    op.create_index(
        "ix_invoices_client_ip_hash_open",
        "invoices",
        ["client_ip_hash"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.drop_constraint("invoices_order_id_key", "invoices", type_="unique")
    op.create_unique_constraint("uq_invoices_account_id_order_id", "invoices", ["account_id", "order_id"])
    op.drop_column("invoices", "webhook_url")

    op.rename_table("owner_events", "account_events")
    op.execute("ALTER INDEX ix_owner_events_invoice_id RENAME TO ix_account_events_invoice_id")
    op.execute("ALTER INDEX ix_owner_events_created_at RENAME TO ix_account_events_created_at")
    op.execute("ALTER TABLE account_events RENAME CONSTRAINT owner_events_pkey TO account_events_pkey")
    op.execute("ALTER SEQUENCE owner_events_id_seq RENAME TO account_events_id_seq")
    op.add_column(
        "account_events",
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_account_events_account_id_id", "account_events", ["account_id", "id"])

    op.drop_table("webhook_deliveries")
    op.execute("DROP TYPE webhook_delivery_status")


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
