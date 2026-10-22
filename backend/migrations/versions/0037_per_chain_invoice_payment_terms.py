"""Per-chain payment window and accepted shortfall: expand only.

- `accounts.invoice_lifetime_seconds_by_chain` and `accounts.accuracy_percent_by_chain`: what the account's invoices
  take per chain key when their maker sets none, `{"polygon": 3600}` and `{"polygon": "0.5"}` (the percent a decimal
  string, never a float); `{}` when unset, so every chain stands at the server's default.
- `invoices.payment_terms_by_chain`: each offered chain's resolved window and shortfall on an invoice whose payer
  still picks among several chains, so the pick takes the chosen chain's. Null on one-chain invoices and on every row
  written before this revision, whose `lifetime_seconds` and `accuracy_percent` then stand for any pick, as before.
- `payment_links` and `invoice_schedules` `lifetime_seconds` and `accuracy_percent` become nullable: null is "the
  account's default for the chain, when each invoice is made". Every existing row keeps its value.

Adding nullable or defaulted columns and dropping NOT NULL change nothing the running release reads on existing rows,
so the step is additive.

Revision ID: 0037
Revises: 0036
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN and ALTER COLUMN take ACCESS EXCLUSIVE on their table
    # (https://www.postgresql.org/docs/current/sql-altertable.html): fail after 5 s rather than queue every read and
    # write behind a long transaction holding one of them. A constant default makes ADD COLUMN a catalog change, with
    # no table rewrite (PostgreSQL 11 and later, same page), and DROP NOT NULL checks no rows.
    op.execute("SET LOCAL lock_timeout = '5s'")
    for column_name in ("invoice_lifetime_seconds_by_chain", "accuracy_percent_by_chain"):
        op.add_column(
            "accounts",
            sa.Column(column_name, postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        )
    op.add_column("invoices", sa.Column("payment_terms_by_chain", postgresql.JSONB(), nullable=True))
    for table_name in ("payment_links", "invoice_schedules"):
        op.alter_column(table_name, "lifetime_seconds", existing_type=sa.Integer(), nullable=True)
        op.alter_column(table_name, "accuracy_percent", existing_type=sa.Numeric(4, 2), nullable=True)


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
