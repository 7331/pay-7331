"""Drop payment_links, invoices.payment_link_id and invoices.client_ip_hash: reusable payment links and the per-IP
open-invoice cap that only counted their invoices are gone.

invoices.payment_link_id never had a foreign key, only its index. Production's rows were backed up before this ran.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_invoices_payment_link_id", table_name="invoices")
    op.drop_column("invoices", "payment_link_id")
    op.drop_index("ix_invoices_client_ip_hash_open", table_name="invoices")
    op.drop_column("invoices", "client_ip_hash")
    op.drop_table("payment_links")


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
