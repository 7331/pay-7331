"""``GET /api/v1/rates``: public, rate limited like every read (120 a minute per network), the chains'
Chainlink prices exactly as invoices are quoted, from the same 30-second Redis copy, so a busy page costs no feed read
of its own."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.lifespan.dependencies import DatabaseSession, RedisDependency, ResourcesDependency
from app.payments.price_quotes.schemas import RateListResponse
from app.payments.price_quotes.service import PriceQuoteService
from app.shared.path_params import API_PREFIX

price_quotes_router = APIRouter(prefix=API_PREFIX, tags=["rates"])

# The answer is already up to 30 s old (the Redis copy, store.py); a cache in between may keep it 10 s more, so a
# rate read through one is at most about 40 s old.
RATES_CACHE_HEADERS = {"Cache-Control": "public, max-age=10"}


def get_price_quote_service(
    session: DatabaseSession, *, redis: RedisDependency, resources: ResourcesDependency
) -> PriceQuoteService:
    """The session opens no connection: a price read touches Redis and the feeds only."""
    return PriceQuoteService.on_session(
        session,
        redis=redis,
        chains_service=resources.invoice_wiring.chains_service,
        chainlink_clients=resources.invoice_wiring.chainlink_clients,
    )


PriceQuoteServiceDependency = Annotated[PriceQuoteService, Depends(get_price_quote_service)]


@price_quotes_router.get("/rates")
async def list_exchange_rates(
    *, response: Response, price_quote_service: PriceQuoteServiceDependency
) -> RateListResponse:
    """Every coin of every chain a new invoice can go to, in ``GET /chains`` order."""
    response.headers.update(RATES_CACHE_HEADERS)
    return await price_quote_service.list_exchange_rates()
