"""At most one active top-up per key it fills, not per key that sends it (2026-10-14; PLAN.md section 5, "Treasury").

A key with too little gas for its own top-up has it sent by another operator key carrying its delegate signature
(settlements/operator_key_maintenance_service.py), so `operator_address` is the sender and `recipient_address` the key
topped up. The unique partial index moves from (`chain_key`, `operator_address`, `asset_symbol`) to
(`chain_key`, `recipient_address`, `asset_symbol`). Every top-up the running release records has the two equal, so
no row breaks the new index and the release keeps working while migrate runs.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_TOP_UP_PREDICATE = "kind = 'top_up' AND status IN ('pending', 'waiting_for_float', 'broadcast', 'mined')"


def upgrade() -> None:
    op.drop_index("ux_outgoing_operations_active_top_up", table_name="outgoing_operations")
    op.create_index(
        "ux_outgoing_operations_active_top_up",
        "outgoing_operations",
        ["chain_key", "recipient_address", "asset_symbol"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_TOP_UP_PREDICATE),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
