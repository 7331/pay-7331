"""One-time passkey enrollment links, the account recovery path (2026-10-15): expand only.

- `account_passkey_enrollment_links`: a link the operator mints for an account (SHA-256 of its token, expiry, when it
  was used). Deleted with the account. A new table the running release never reads, so the step is additive.

Revision ID: 0029
Revises: 0028
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_passkey_enrollment_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_account_passkey_enrollment_links_account_id", "account_passkey_enrollment_links", ["account_id"]
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
