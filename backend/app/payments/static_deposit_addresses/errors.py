"""Static deposit address refusals, each a stable code the pages map to their own sentence."""

from app.shared.errors import ConflictError, NotFoundError


class StaticDepositAddressNotFoundError(NotFoundError):
    code = "static_deposit_address_not_found"


class StaticDepositAddressLimitReachedError(ConflictError):
    """The account holds ``PAY_MAXIMUM_STATIC_DEPOSIT_ADDRESSES_PER_ACCOUNT`` already."""

    code = "static_deposit_address_limit_reached"
