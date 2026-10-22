"""An automatic refund that waits for a price is decided again every tick (2026-10-12; PLAN.md section 6).

- `invoices.automatic_refund_deferred_since`: set when the overpay excess or stray money could not be decided for
  want of a price, cleared once decided. Null for every existing invoice: no past decision is re-run, since the owner
  may have settled one by hand.
- `ix_invoices_chain_key_automatic_refund_deferred`: the watcher's one candidate query a tick.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("automatic_refund_deferred_since", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_invoices_chain_key_automatic_refund_deferred",
        "invoices",
        ["chain_key"],
        postgresql_where=sa.text("automatic_refund_deferred_since IS NOT NULL"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
