"""Refreshed orders, per-invoice callbacks and the payer's after-payment link (2026-10-21).

- `invoices.refreshed_from_invoice_id` (a foreign key to `invoices.id`) and `invoices.refreshed_into_invoice_id`: a
  refresh (`refresh_expired` on `POST /invoices`) makes a new invoice for an expired, unpaid order; the two point at
  each other and the old keeps its order id, deposit and rules (PLAN.md section 4).
- `ux_invoices_account_id_order_id_current`, unique `(account_id, order_id)` over rows no refresh replaced, replaces
  `uq_invoices_account_id_order_id`: one current invoice per order. The running release never sets
  `refreshed_into_invoice_id`, so for it the two are the same rule.
- `invoices.success_url` (the payer's link after paying) and `invoices.callback_url` (the invoice's own signed
  receiver of its events).
- `account_webhook_deliveries.receiver_url`: an `invoice_callback` delivery's URL, fixed when queued.

All new columns are nullable and the running release reads none of them, so the step is additive.

Revision ID: 0032
Revises: 0031
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("success_url", sa.String(2048), nullable=True))
    op.add_column("invoices", sa.Column("callback_url", sa.String(2048), nullable=True))
    op.add_column(
        "invoices",
        sa.Column(
            "refreshed_from_invoice_id",
            sa.String(26),
            sa.ForeignKey("invoices.id", name="fk_invoices_refreshed_from_invoice_id_invoices"),
            nullable=True,
        ),
    )
    op.add_column("invoices", sa.Column("refreshed_into_invoice_id", sa.String(26), nullable=True))
    op.create_index(
        "ux_invoices_account_id_order_id_current",
        "invoices",
        ["account_id", "order_id"],
        unique=True,
        postgresql_where=sa.text("refreshed_into_invoice_id IS NULL"),
    )
    op.drop_constraint("uq_invoices_account_id_order_id", "invoices", type_="unique")
    op.add_column("account_webhook_deliveries", sa.Column("receiver_url", sa.String(2048), nullable=True))


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
