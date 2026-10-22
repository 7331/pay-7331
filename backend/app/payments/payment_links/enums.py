"""A quantity link's status as the pages show it."""

from enum import StrEnum


class PaymentLinkStatus(StrEnum):
    OPEN = "open"
    # No slot free right now, but some are held by open invoices that may expire unpaid and free them.
    ALL_TAKEN = "all_taken"
    # Every slot is paid.
    SOLD_OUT = "sold_out"
    # Closed by its account (or its account is gone).
    CLOSED = "closed"
