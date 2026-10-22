"""Add owner_passkeys, owner_sessions and owner_passkey_challenges: owner sign-in with passkeys (PLAN.md section 7).

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def audit_timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "owner_passkeys",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("transports", postgresql.ARRAY(sa.String(length=16)), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *audit_timestamp_columns(),
        sa.UniqueConstraint("credential_id"),
    )
    op.create_table(
        "owner_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "owner_passkey_id",
            sa.BigInteger(),
            sa.ForeignKey("owner_passkeys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *audit_timestamp_columns(),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_owner_sessions_owner_passkey_id", "owner_sessions", ["owner_passkey_id"])
    op.create_index("ix_owner_sessions_expires_at", "owner_sessions", ["expires_at"])
    op.create_table(
        "owner_passkey_challenges",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("challenge_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *audit_timestamp_columns(),
        sa.UniqueConstraint("challenge_hash"),
    )
    op.create_index("ix_owner_passkey_challenges_expires_at", "owner_passkey_challenges", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
