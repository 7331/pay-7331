"""Operator key pool and treasury top-ups (2026-10-13; PLAN.md section 5, treasury).

- `operation_kind` gains `top_up` (a key pulls funds from the treasury Safe through the Allowance Module),
  `treasury_return` (a retired key sends what it holds back to the Safe) and `treasury_safe_creation` (the refund key
  deploys the Safe). Postgres refuses a new enum value inside the transaction that added it ("unsafe use of new
  value", https://www.postgresql.org/docs/current/sql-altertype.html), so they are added in an autocommit block before
  the index that names `top_up`.
- `outgoing_operations.invoice_id` becomes nullable: the key operations belong to no invoice. A relaxation, so no
  expand and contract; every existing row keeps its invoice.
- A unique partial index keeps at most one active top-up per chain, key and asset.
- `operator_key_states`: each operator key's role (refund or sweeper) and status (active, frozen, retired), the
  reason and when it changed.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for new_kind in ("top_up", "treasury_return", "treasury_safe_creation"):
            op.execute(f"ALTER TYPE operation_kind ADD VALUE IF NOT EXISTS '{new_kind}'")
    op.alter_column("outgoing_operations", "invoice_id", existing_type=sa.String(26), nullable=True)
    op.create_index(
        "ux_outgoing_operations_active_top_up",
        "outgoing_operations",
        ["chain_key", "operator_address", "asset_symbol"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'top_up' AND status IN ('pending', 'waiting_for_float', 'broadcast', 'mined')"
        ),
    )
    operator_key_role = postgresql.ENUM("refund", "sweeper", name="operator_key_role")
    operator_key_status = postgresql.ENUM("active", "frozen", "retired", name="operator_key_status")
    op.create_table(
        "operator_key_states",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("chain_key", sa.String(32), nullable=False),
        sa.Column("operator_address", sa.String(42), nullable=False),
        sa.Column("role", operator_key_role, nullable=False),
        sa.Column("status", operator_key_status, nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ux_operator_key_states_chain_address", "operator_key_states", ["chain_key", "operator_address"], unique=True
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
