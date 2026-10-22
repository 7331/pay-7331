"""Invoice schedules (PLAN.md section 18): one account's recurring invoice terms, from which the watcher makes one
invoice per period as it comes due (``invoices.invoice_schedule_id`` and ``invoice_schedule_period_start``, unique
together). Push only: each invoice is paid by its payer sending, like any other; nothing is stored that could take
money."""

from datetime import datetime
from decimal import Decimal

from pydantic import JsonValue
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base

ADDRESS_LENGTH = 42


class InvoiceSchedule(Base):
    __tablename__ = "invoice_schedules"
    __table_args__ = (
        Index("ix_invoice_schedules_account_id_created_at_id", "account_id", "created_at", "id"),
        # The watcher's due query: active schedules by their next attempt.
        Index(
            "ix_invoice_schedules_next_attempt_at_active",
            "next_attempt_at",
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint("status IN ('active', 'paused', 'cancelled', 'completed')", name="ck_invoice_schedules_status"),
        CheckConstraint(
            "billing_interval IN ('daily', 'weekly', 'monthly')", name="ck_invoice_schedules_billing_interval"
        ),
        CheckConstraint("ends_after_invoice_count IS NULL OR ends_at IS NULL", name="ck_invoice_schedules_one_end"),
    )

    # `sch_` and 22 base58 characters (app/shared/identifiers.py).
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    # None once its account is deleted: the schedule then makes nothing.
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16))
    # The invoice terms, as ``POST /invoices`` takes them and resolved the same way when the schedule was made
    # (``NewInvoice``): fixed on the schedule, so a later settings change moves none of its invoices.
    amount: Mapped[str] = mapped_column(String(40))
    # USD, EUR or a coin symbol.
    currency: Mapped[str] = mapped_column(String(16))
    chain_keys: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    defaulted_chains: Mapped[bool] = mapped_column(Boolean)
    assets: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    assets_by_chain: Mapped[dict[str, list[str]] | None] = mapped_column(JSONB)
    # Checked when the schedule was made; a later payout address change does not move it (as a quantity link's).
    destination_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    note: Mapped[str] = mapped_column(String(500), server_default=text("''"))
    # "metadata" is reserved on declarative classes; the column keeps the API's name. Copied onto every invoice.
    private_metadata: Mapped[dict[str, JsonValue] | None] = mapped_column("metadata", JSONB)
    # Each invoice's order id is this, a dash and its period's start day (`plan-77-20261021`); the schedule's id when
    # left out.
    order_id_prefix: Mapped[str | None] = mapped_column(String(100))
    lifetime_seconds: Mapped[int] = mapped_column(Integer)
    accuracy_percent: Mapped[Decimal] = mapped_column(Numeric(4, 2))
    return_url: Mapped[str | None] = mapped_column(String(2048))
    success_url: Mapped[str | None] = mapped_column(String(2048))
    callback_url: Mapped[str | None] = mapped_column(String(2048))
    billing_interval: Mapped[str] = mapped_column(String(16))
    # Period n starts n intervals after this (monthly: on this day of the month, clamped to the month's end), UTC.
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # At most one end: after this many invoices, or no period starting at or after this time.
    ends_after_invoice_count: Mapped[int | None] = mapped_column(Integer)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The first period not billed yet, and when the watcher next tries: that period's start, or later after a failure.
    next_period_index: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_invoice_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_invoice_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The refusal code of the last failed attempt (``price_unavailable``, ``webhook_not_set``, ...), cleared by the next
    # invoice made; failures in a row space the retries out.
    last_failure_code: Mapped[str | None] = mapped_column(String(64))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
