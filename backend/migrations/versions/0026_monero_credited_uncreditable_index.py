"""The late pass finds credited Monero rows no longer creditable after a reorg (2026-10-14; PLAN.md section 14).

`ix_monero_payments_chain_key_credited_uncreditable`: a partial index on `monero_payments (chain_key)` where
`credited_at IS NOT NULL AND (block_height IS NULL OR cross_check_state != 'confirmed')`. It also finds a row
re-mined with a pending or mismatching first check before the watcher read the reorg. A row matches only between
a reorg and the watcher's next pass, so
the index stays near empty and the watcher's read of a closed invoice's reorged late payment does not grow with every
payment ever recorded. An index the running release does not read, so the step is additive.

Revision ID: 0026
Revises: 0025
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_monero_payments_chain_key_credited_uncreditable",
        "monero_payments",
        ["chain_key"],
        postgresql_where=sa.text(
            "credited_at IS NOT NULL AND (block_height IS NULL OR cross_check_state != 'confirmed')"
        ),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
