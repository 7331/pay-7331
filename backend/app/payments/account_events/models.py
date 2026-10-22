"""Account events (PLAN.md section 8.1): every invoice and refund event, kept so the invoice's account can stream them,
and the account's own changes (its payout address). Events of an invoice without an account are kept too and streamed
to nobody."""

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base
from app.shared.identifiers import generate_public_id

ACCOUNT_EVENT_PUBLIC_ID_PREFIX = "evt_"


class AccountEvent(Base):
    __tablename__ = "account_events"
    __table_args__ = (
        Index("ix_account_events_created_at", "created_at"),
        Index("ix_account_events_account_id_id", "account_id", "id"),
        Index("ix_account_events_webhook_unclaimed", "id", postgresql_where=text("webhook_claimed_at IS NULL")),
        Index("ux_account_events_public_id", "public_id", unique=True),
    )

    # Sequential and internal: orders the feed; page cursors and `Last-Event-ID` (public ids) map back to it.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The event's id on the wire (`evt_...`, migration 0036): the SSE `id`, `Last-Event-ID` and the webhook-id.
    public_id: Mapped[str] = mapped_column(
        String(26),
        default=lambda: generate_public_id(ACCOUNT_EVENT_PUBLIC_ID_PREFIX),
        server_default=text(f"generate_public_id('{ACCOUNT_EVENT_PUBLIC_ID_PREFIX}')"),
    )
    # None for an event about the account itself (`account.payout_address_changed`; migration 0011).
    invoice_id: Mapped[str | None] = mapped_column(String(26), index=True)
    # The invoice's account; None for an invoice made signed out (or whose account was deleted).
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(64))
    # The account's Invoice resource as it was at the change; for an account event, what changed.
    data: Mapped[JsonValue] = mapped_column(JSONB)
    # The insert's own time, not its transaction's start: the stream re-reads a recent window by it.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    # When the API's webhook loop took the row (payments/account_webhooks; migration 0015), whether or not the account
    # has a webhook: every row is taken once, so the unclaimed set (a partial index) stays small. Rows before 0015
    # were marked taken by the migration.
    webhook_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperatorAlertProblem(Base):
    """A problem an ``operator.alert`` was raised for, while it lasts (migration 0031): it alerts
    when it opens (``created_at``) and again only at the operator's reminder cadence. Gone once not seen for
    ``OPERATOR_ALERT_PROBLEM_CLEARED_AFTER`` (operator_alert_recorder.py), so a problem that comes back alerts anew."""

    __tablename__ = "operator_alert_problems"

    problem_key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_alerted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
