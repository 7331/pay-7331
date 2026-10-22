"""`invoices.client_ipv6_48_hash`: the salted hash of the IPv6 /48 a web invoice was made from, so the open-invoice
cap can count a whole /48 on top of each /64 (`PAY_MAXIMUM_OPEN_INVOICES_PER_IPV6_48`). NULL for an IPv4 client and
for every invoice made before this revision, which the /48 count leaves out until they close.

`ix_invoices_client_ipv6_48_hash_open` serves that count over the open rows only, as `ix_invoices_client_ip_hash_open`
does for the /64.

A nullable column without a default and an index the running release does not read, so the step is additive.

Revision ID: 0035
Revises: 0034
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN takes ACCESS EXCLUSIVE on invoices (https://www.postgresql.org/docs/current/sql-altertable.html):
    # fail after 5 s rather than queue every invoice read and write behind a long transaction holding invoices.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("invoices", sa.Column("client_ipv6_48_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_invoices_client_ipv6_48_hash_open",
        "invoices",
        ["client_ipv6_48_hash"],
        postgresql_where=sa.text("closed_at IS NULL AND client_ipv6_48_hash IS NOT NULL"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
