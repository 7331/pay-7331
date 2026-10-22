"""PriceQuoteService: Chainlink prices per chain and the quotes made from them (PLAN.md section 4).

On a rollup the read also gates on the chain's L2 Sequencer Uptime Feed (``chainlink_api``, PLAN.md section 15): no
price while the sequencer is down or for an hour after it is back.

A price is the feed's answer (> 0) scaled by its decimals and refused when older than its asset's
``maximum_price_age_seconds`` (the chain's for EUR/USD and for an asset that sets none) on every endpoint that
answers. A quote converts a fiat amount to the asset's base
units, rounded up, and holds until ``expires_at``; a new quote for an invoice supersedes its live one, so one quote is
live at a time.
"""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
import time
from typing import Self

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.chainlink_api.client import ChainlinkApiClient
from app.infrastructure.chainlink_api.errors import ChainlinkFeedError
from app.payments.chains.service import ChainsService
from app.payments.chains.types import AssetConfiguration, ChainConfiguration
from app.payments.price_quotes.errors import PriceUnavailableError
from app.payments.price_quotes.log_events import PriceCacheUnavailable, PriceFeedStale, PriceFeedUnreadable
from app.payments.price_quotes.models import PriceQuote
from app.payments.price_quotes.repository import PriceQuoteRepository
from app.payments.price_quotes.schemas import AssetRateResponse, RateListResponse
from app.payments.price_quotes.store import PriceStore
from app.payments.price_quotes.types import ChainPrices, QuoteValue
from app.shared import logging as log
from app.shared.amounts import ceil_to_base_units, format_decimal_text

logger = log.get_logger(__name__)


def quote_value_of(quote: PriceQuote) -> QuoteValue:
    return QuoteValue(
        quote_id=quote.id,
        asset_symbol=quote.asset_symbol,
        fiat_currency=quote.fiat_currency,
        asset_price_usd=quote.asset_price_usd,
        fiat_price_usd=quote.fiat_price_usd,
        eur_price_usd=quote.eur_price_usd,
        fiat_amount=quote.fiat_amount,
        amount_base_units=quote.amount_base_units,
        created_at=quote.created_at,
        expires_at=quote.expires_at,
        superseded_at=quote.superseded_at,
    )


# EUR per coin is a quotient: 8 decimals, as Chainlink's USD feeds answer.
EUR_RATE_QUANTUM = Decimal("0.00000001")


# How far a pegged stablecoin's feed may sit from 1 USD and still count as exactly 1 USD.
USD_PEG_TOLERANCE = Decimal("0.005")


def pegged_price_usd(asset: AssetConfiguration, *, feed_price_usd: Decimal) -> Decimal:
    """A pegged stablecoin is worth exactly 1 USD while its feed is within ``USD_PEG_TOLERANCE`` of the peg, so a 5 USD
    invoice asks 5.00 USDC; off the peg it is worth the feed price but never above 1 USD, so a
    depeg below asks more coins and a feed above the peg never asks fewer than the fiat amount."""
    if not asset.usd_peg:
        return feed_price_usd
    if abs(feed_price_usd - 1) <= USD_PEG_TOLERANCE:
        return Decimal(1)
    return min(feed_price_usd, Decimal(1))


class PriceQuoteService:
    def __init__(
        self,
        *,
        repository: PriceQuoteRepository,
        store: PriceStore,
        chains_service: ChainsService,
        chainlink_clients: dict[str, ChainlinkApiClient],
    ) -> None:
        self.repository = repository
        self.store = store
        self.chains_service = chains_service
        self.chainlink_clients = chainlink_clients

    @classmethod
    def on_session(
        cls,
        session: AsyncSession,
        *,
        redis: Redis,
        chains_service: ChainsService,
        chainlink_clients: dict[str, ChainlinkApiClient],
    ) -> Self:
        return cls(
            repository=PriceQuoteRepository(session=session),
            store=PriceStore(client=redis),
            chains_service=chains_service,
            chainlink_clients=chainlink_clients,
        )

    async def read_chain_prices(self, chain_key: str) -> ChainPrices:
        """Every configured asset's USD price and EUR/USD, from Redis when a copy is fresh, else from the feeds. A
        read that just found none fresh is answered from its short Redis mark instead (``store.py``)."""
        chain = self.chains_service.get_chain(chain_key)
        try:
            cached = await self.store.find_chain_prices(chain_key)
            marked_unavailable = cached is None and await self.store.is_marked_unavailable(chain_key)
        except RedisError:
            log.emit(logger, event=PriceCacheUnavailable())
            cached = None
            marked_unavailable = False
        # A copy written before an asset was added to chains.toml lacks it: read the feeds instead (2026-10-11).
        if cached is not None and all(asset.symbol in cached.asset_prices_usd for asset in chain.assets):
            return cached
        if marked_unavailable:
            raise PriceUnavailableError("prices are unavailable right now; try again shortly")
        try:
            prices = await self.read_feeds(chain)
        except PriceUnavailableError:
            try:
                await self.store.mark_unavailable(chain_key)
            except RedisError:
                log.emit(logger, event=PriceCacheUnavailable())
            raise
        try:
            await self.store.write_chain_prices(prices)
        except RedisError:
            log.emit(logger, event=PriceCacheUnavailable())
        return prices

    async def read_feeds(self, chain: ChainConfiguration) -> ChainPrices:
        feed_addresses = [asset.usd_feed for asset in chain.assets] + [chain.eur_usd_feed]
        # Each asset feed's own limit (XMR/USD is a 24 h feed), EUR/USD the chain's.
        maximum_age_seconds_by_feed = [asset.maximum_price_age_seconds for asset in chain.assets] + [
            chain.maximum_price_age_seconds
        ]
        try:
            # A stale answer moves the read to the next endpoint first, so one endpoint lagging behind the tip refuses
            # no price; a reading still stale below is stale on every endpoint that answered (2026-10-13).
            readings = await self.chainlink_clients[chain.key].read_feeds(
                feed_addresses,
                maximum_age_seconds_by_feed=maximum_age_seconds_by_feed,
                sequencer_uptime_feed=chain.sequencer_uptime_feed,
            )
        except ChainlinkFeedError as exc:
            log.emit(logger, event=PriceFeedUnreadable(chain_key=chain.key))
            raise PriceUnavailableError("prices are unavailable right now; try again shortly") from exc
        now_timestamp = int(time.time())
        for reading, maximum_age_seconds in zip(readings, maximum_age_seconds_by_feed, strict=True):
            age_seconds = now_timestamp - reading.updated_at_timestamp
            if age_seconds > maximum_age_seconds:
                log.emit(
                    logger,
                    event=PriceFeedStale(
                        chain_key=chain.key, feed_address=reading.feed_address, age_seconds=age_seconds
                    ),
                )
                raise PriceUnavailableError("a price feed is stale; try again later")
        return ChainPrices(
            chain_key=chain.key,
            asset_prices_usd={
                asset.symbol: pegged_price_usd(asset, feed_price_usd=reading.price)
                for asset, reading in zip(chain.assets, readings, strict=False)
            },
            eur_price_usd=readings[-1].price,
        )

    async def list_exchange_rates(self) -> RateListResponse:
        """``GET /rates``: each coin of each chain that takes invoices at the price a quote would use now; a chain
        whose feeds do not answer lists its coins with null prices, and the other chains are unaffected."""
        rates: list[AssetRateResponse] = []
        for chain in self.chains_service.chains:
            if not chain.takes_invoices:
                continue
            try:
                prices: ChainPrices | None = await self.read_chain_prices(chain.key)
            except PriceUnavailableError:
                prices = None
            for asset in chain.assets:
                price_usd = prices.asset_prices_usd.get(asset.symbol) if prices is not None else None
                price_eur = (
                    (price_usd / prices.eur_price_usd).quantize(EUR_RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
                    if price_usd is not None and prices is not None
                    else None
                )
                rates.append(
                    AssetRateResponse(
                        chain=chain.key,
                        asset=asset.symbol,
                        usd=format_decimal_text(price_usd) if price_usd is not None else None,
                        eur=format_decimal_text(price_eur) if price_eur is not None else None,
                    )
                )
        return RateListResponse(rates=rates)

    async def create_quote(
        self,
        invoice_id: str,
        *,
        chain_key: str,
        asset: AssetConfiguration,
        fiat_currency: str,
        fiat_amount: Decimal,
        lifetime_seconds: int,
    ) -> QuoteValue:
        """Supersede the invoice's live quote and make a new one for ``fiat_amount``, live for the lifetime."""
        prices = await self.read_chain_prices(chain_key)
        asset_price_usd = prices.asset_prices_usd[asset.symbol]
        fiat_price_usd = prices.fiat_price_usd(fiat_currency)
        now = datetime.now(UTC)
        await self.repository.supersede_live_quotes(invoice_id, superseded_at=now)
        quote = await self.repository.insert_quote(
            PriceQuote(
                invoice_id=invoice_id,
                chain_key=chain_key,
                asset_symbol=asset.symbol,
                fiat_currency=fiat_currency,
                asset_price_usd=asset_price_usd,
                fiat_price_usd=fiat_price_usd,
                eur_price_usd=prices.eur_price_usd,
                fiat_amount=fiat_amount,
                amount_base_units=ceil_to_base_units(
                    fiat_amount * fiat_price_usd / asset_price_usd,
                    decimals=asset.decimals,
                    quote_decimals=asset.quote_decimals,
                ),
                created_at=now,
                expires_at=now + timedelta(seconds=lifetime_seconds),
            )
        )
        return quote_value_of(quote)

    async def list_quotes(self, invoice_id: str) -> list[QuoteValue]:
        return [quote_value_of(quote) for quote in await self.repository.list_quotes_for_invoice(invoice_id)]

    async def list_quotes_by_invoice(self, invoice_ids: list[str]) -> dict[str, list[QuoteValue]]:
        """Every quote of each invoice, oldest first, in one read (the CSV export)."""
        quotes_by_invoice: dict[str, list[QuoteValue]] = {invoice_id: [] for invoice_id in invoice_ids}
        for quote in await self.repository.list_quotes_for_invoices(invoice_ids):
            quotes_by_invoice[quote.invoice_id].append(quote_value_of(quote))
        return quotes_by_invoice

    async def find_latest_quote(self, invoice_id: str) -> QuoteValue | None:
        quote = await self.repository.find_latest_quote(invoice_id)
        return quote_value_of(quote) if quote is not None else None
