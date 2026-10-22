"""Add owner_passkey_enrollment_tokens (the CLI's one-time `/enroll#<token>` link) and drop api_keys: the owner signs
in only with a passkey, no API keys at all (PLAN.md section 7).

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "owner_passkey_enrollment_tokens",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_owner_passkey_enrollment_tokens_expires_at", "owner_passkey_enrollment_tokens", ["expires_at"])
    op.drop_table("api_keys")


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
