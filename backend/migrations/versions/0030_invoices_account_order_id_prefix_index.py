"""`GET /invoices?order_id_prefix=` reads one account's invoices by order id prefix (2026-10-15).

`ix_invoices_account_id_order_id_pattern`: `invoices (account_id, order_id varchar_pattern_ops)`. The database
collation is not C, so `uq_invoices_account_id_order_id` cannot serve an anchored `LIKE`; the pattern operator class
can (https://www.postgresql.org/docs/18/indexes-opclass.html). An index the running release does not read, so the
step is additive.

Revision ID: 0030
Revises: 0029
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_invoices_account_id_order_id_pattern",
        "invoices",
        ["account_id", "order_id"],
        postgresql_ops={"order_id": "varchar_pattern_ops"},
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
