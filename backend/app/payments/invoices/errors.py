"""Invoice refusals, each a stable code the pages map to their own copy."""

from app.shared.errors import AppError, ConflictError, InvalidInputError, NotFoundError, RateLimitedError


class InvoiceNotRefreshableError(ConflictError):
    """``refresh_expired`` on an order whose expired invoice got money (a payment, late or stray money): a fresh
    invoice could make the payer pay twice, so the account settles that one first (2026-10-21)."""

    code = "invoice_not_refreshable"


class TooManyOpenInvoicesError(RateLimitedError):
    """The client network already has ``PAY_MAXIMUM_OPEN_INVOICES_PER_IP`` unclosed invoices, or its IPv6 /48
    ``PAY_MAXIMUM_OPEN_INVOICES_PER_IPV6_48`` (PLAN.md section 9)."""

    code = "too_many_open_invoices"


class PublicInvoiceCapacityReachedError(RateLimitedError):
    """A chain the public invoice offers already has ``PAY_MAXIMUM_OPEN_PUBLIC_INVOICES_PER_CHAIN`` unclosed public
    invoices (PLAN.md section 9)."""

    code = "public_invoice_capacity_reached"


class DestinationRequiredError(InvalidInputError):
    """Signed out, the receiver address must be pasted: there is no default receiver (2026-10-09)."""

    code = "destination_required"


class InvoiceNotFoundError(NotFoundError):
    code = "invoice_not_found"


class InvoiceStateError(ConflictError):
    """The invoice's status does not allow the action."""

    code = "invoice_state_conflict"


class DestinationRefusedError(InvalidInputError):
    code = "destination_refused"


class FactoryNotDeployedError(AppError):
    code = "factory_not_deployed"
    status = 503


class RefundNotAllowedError(ConflictError):
    code = "refund_not_allowed"


class StrayTransactionRefusedError(ConflictError):
    """A transaction hash that cannot be recorded as stray money (not final, not into a deposit, the invoice's own
    coin, already recorded; INC-001)."""

    code = "stray_transaction_refused"
