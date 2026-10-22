"""An invoice schedule's interval and status."""

from enum import StrEnum


class InvoiceScheduleInterval(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    # On the start's day of the month, the month's last day when it has fewer (the 31st bills on 30 April).
    MONTHLY = "monthly"


class InvoiceScheduleStatus(StrEnum):
    # Makes an invoice as each period comes due.
    ACTIVE = "active"
    # Makes none; a period that starts while paused is never billed.
    PAUSED = "paused"
    # Ended by its account; invoices already made run their course.
    CANCELLED = "cancelled"
    # Ended by its own end: its invoice count or end date was reached.
    COMPLETED = "completed"

    @property
    def is_ended(self) -> bool:
        return self in {InvoiceScheduleStatus.CANCELLED, InvoiceScheduleStatus.COMPLETED}
