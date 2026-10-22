"""Price failures callers tell apart."""

from app.shared.errors import AppError


class PriceUnavailableError(AppError):
    """No fresh Chainlink answer (a feed is older than its maximum_price_age_seconds, the asset's or the chain's, on
    every endpoint that answers, or unreachable)."""

    code = "price_unavailable"
    status = 503
