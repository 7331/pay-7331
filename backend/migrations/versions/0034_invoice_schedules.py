"""Invoice schedules (2026-10-21, PLAN.md section 18): an account's recurring invoice terms, from which the watcher
makes one ordinary invoice per period as it comes due. Push only: nothing stored here can take money.

- `invoice_schedules`: the terms as `POST /invoices` takes them (resolved once), the interval, start and end, the next
  period and attempt, the invoices made and the last failure.
- `invoices.invoice_schedule_id` (a foreign key to `invoice_schedules.id`; no ORM one, as `payment_link_id`) and
  `invoices.invoice_schedule_period_start`, with `ux_invoices_invoice_schedule_id_period_start`, unique over the pair
  where a schedule made the invoice: one invoice per schedule and period, however many runners try.

All new columns are nullable and the running release reads none of them, so the step is additive.

Revision ID: 0034
Revises: 0033
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN ... REFERENCES takes ACCESS EXCLUSIVE on invoices
    # (https://www.postgresql.org/docs/current/sql-altertable.html): fail after 5 s rather than queue every invoice
    # read and write behind a long transaction holding invoices.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "invoice_schedules",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("amount", sa.String(40), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("chain_keys", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("defaulted_chains", sa.Boolean(), nullable=False),
        sa.Column("assets", postgresql.ARRAY(sa.String(16)), nullable=False),
        sa.Column("assets_by_chain", postgresql.JSONB(), nullable=True),
        sa.Column("destination_address", sa.String(42), nullable=False),
        sa.Column("note", sa.String(500), server_default=sa.text("''"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("order_id_prefix", sa.String(100), nullable=True),
        sa.Column("lifetime_seconds", sa.Integer(), nullable=False),
        sa.Column("accuracy_percent", sa.Numeric(4, 2), nullable=False),
        sa.Column("return_url", sa.String(2048), nullable=True),
        sa.Column("success_url", sa.String(2048), nullable=True),
        sa.Column("callback_url", sa.String(2048), nullable=True),
        sa.Column("billing_interval", sa.String(16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_after_invoice_count", sa.Integer(), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_period_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_invoice_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_invoice_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.String(64), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'cancelled', 'completed')", name="ck_invoice_schedules_status"
        ),
        sa.CheckConstraint(
            "billing_interval IN ('daily', 'weekly', 'monthly')", name="ck_invoice_schedules_billing_interval"
        ),
        sa.CheckConstraint("ends_after_invoice_count IS NULL OR ends_at IS NULL", name="ck_invoice_schedules_one_end"),
    )
    op.create_index(
        "ix_invoice_schedules_account_id_created_at_id", "invoice_schedules", ["account_id", "created_at", "id"]
    )
    op.create_index(
        "ix_invoice_schedules_next_attempt_at_active",
        "invoice_schedules",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "invoice_schedule_id",
            sa.String(26),
            sa.ForeignKey("invoice_schedules.id", name="fk_invoices_invoice_schedule_id_invoice_schedules"),
            nullable=True,
        ),
    )
    op.add_column("invoices", sa.Column("invoice_schedule_period_start", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ux_invoices_invoice_schedule_id_period_start",
        "invoices",
        ["invoice_schedule_id", "invoice_schedule_period_start"],
        unique=True,
        postgresql_where=sa.text("invoice_schedule_id IS NOT NULL"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
