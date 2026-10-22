"""Add accounts.accepted_asset_symbols_by_chain: the coins an account's invoices start with ticked, saved from the
coins it ticks when it makes an invoice and editable on /settings (PLAN.md section 7).

JSONB keyed by chain key (`{"polygon": ["POL", "USDC"]}`), not one list for the first chain: the enabled chains differ
per deployment (polygon, polygon_amoy, local_fork) and a second chain must not reuse the first's symbols. Empty means
the chains' defaults, so existing accounts need no backfill.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "accepted_asset_symbols_by_chain",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
