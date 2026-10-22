"""Quantity link refusals, each a stable code the pages map to their own copy."""

from app.shared.errors import ConflictError, NotFoundError


class PaymentLinkNotFoundError(NotFoundError):
    code = "payment_link_not_found"


class PaymentLinkSoldOutError(ConflictError):
    """Every slot is paid or held by an open invoice."""

    code = "sold_out"


class PaymentLinkClosedError(ConflictError):
    code = "payment_link_closed"
