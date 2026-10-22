"""``GET /api/v1/rates`` (2026-10-21): each enabled coin's price as the invoices are quoted at."""

from app.shared.schemas import ApiResponse


class AssetRateResponse(ApiResponse):
    # The chain's key, as in `GET /chains`; the same coin on two chains has a row (and a feed) on each.
    chain: str
    asset: str
    # Decimal strings: USD per whole coin (a pegged stablecoin's 1 within 0.5% of the peg, never above 1 off it) and
    # EUR per whole coin (that over the chain's EUR/USD feed, 8 decimals). Both null while the chain's feeds are stale
    # or unreadable.
    usd: str | None
    eur: str | None


class RateListResponse(ApiResponse):
    rates: list[AssetRateResponse]
