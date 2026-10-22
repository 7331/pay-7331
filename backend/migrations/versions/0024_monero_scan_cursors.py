"""The Monero scanner's cursor (2026-10-14; PLAN.md section 14): expand only.

- `monero_scan_cursors`: one row per Monero chain, the last block the xmr-scanner sidecar scanned and its hash. The
  sidecar kept its cursor in memory until now, so a restart with no payment rows skipped the blocks mined while it
  was down; it now resumes after this row, and starts at the tip only when there is none. `created_at` and
  `updated_at` default to `now()` (the declarative base's audit columns): the sidecar's upsert names neither on insert.

A new table nothing in the running release reads, so the step is additive.

Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monero_scan_cursors",
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column("scanned_height", sa.BigInteger(), nullable=False),
        sa.Column("scanned_block_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chain_key"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
