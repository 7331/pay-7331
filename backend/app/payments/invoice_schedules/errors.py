"""Invoice schedule refusals, each a stable code the pages map to their own copy."""

from app.shared.errors import ConflictError, NotFoundError


class InvoiceScheduleNotFoundError(NotFoundError):
    code = "invoice_schedule_not_found"


class InvoiceScheduleStateError(ConflictError):
    """Pause of a schedule that is not active, resume of one that is not paused, either on an ended one."""

    code = "invoice_schedule_state_conflict"


class TooManyInvoiceSchedulesError(ConflictError):
    """``PAY_MAXIMUM_INVOICE_SCHEDULES_PER_ACCOUNT`` schedules that have not ended (active or paused)."""

    code = "too_many_invoice_schedules"


class ScheduledOrderIdTakenError(ConflictError):
    """A period's order id (``<prefix>-<YYYYMMDD>``) is already another invoice's of the account: the schedule cannot
    bill that period without answering that invoice instead. Recorded on the schedule, never answered to a request."""

    code = "order_id_taken"


class OrderIdPrefixTakenError(ConflictError):
    """Another schedule of the account that has not ended makes order ids with this prefix: the two would make the same
    order id on the same day, and a repeated order id answers the invoice already made."""

    code = "order_id_prefix_taken"
