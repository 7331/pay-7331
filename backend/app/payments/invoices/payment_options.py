"""The coins an invoice or a quantity link offers across chains, each with its estimate.

Every chain is priced on its own: one chain's feeds being stale, its sequencer down or its RPC away leaves that chain's
options without an amount and the other chains' untouched (``PaymentOptionReader``).
"""

import asyncio
from decimal import Decimal

from app.payments.chains.service import present_asset, present_chain
from app.payments.chains.types import ChainConfiguration
from app.payments.invoices.schemas import PaymentOption
from app.payments.price_quotes.errors import PriceUnavailableError
from app.payments.price_quotes.service import PriceQuoteService
from app.payments.price_quotes.types import ChainPrices
from app.shared.amounts import ceil_to_base_units, format_base_units


def estimate_fiat_amount_by_asset_symbol(
    fiat_amount: Decimal,
    *,
    fiat_currency: str,
    asset_symbols: list[str],
    chain: ChainConfiguration,
    prices: ChainPrices,
) -> dict[str, str]:
    """Each priced asset's amount for ``fiat_amount`` at the live feed price, rounded as a quote is, as the API's
    decimal text."""
    fiat_price_usd = prices.fiat_price_usd(fiat_currency)
    amount_text_by_symbol: dict[str, str] = {}
    for asset_symbol in asset_symbols:
        asset = chain.find_asset(asset_symbol)
        price_usd = prices.asset_prices_usd.get(asset_symbol)
        if asset is None or price_usd is None or price_usd <= 0:
            continue
        amount_base_units = ceil_to_base_units(
            fiat_amount * fiat_price_usd / price_usd,
            decimals=asset.decimals,
            quote_decimals=asset.quote_decimals,
        )
        amount_text_by_symbol[asset_symbol] = format_base_units(amount_base_units, decimals=asset.decimals)
    return amount_text_by_symbol


def present_payment_options(
    chains_by_key: dict[str, ChainConfiguration],
    *,
    offered_assets_by_chain: dict[str, list[str]],
    prices_by_chain: dict[str, ChainPrices],
    fiat_amount: Decimal,
    fiat_currency: str,
) -> list[PaymentOption]:
    """One option per offered coin of each chain, in the maker's order. ``prices_by_chain`` lacks a chain whose prices
    could not be read: its options have no amount."""
    options: list[PaymentOption] = []
    for chain_key, asset_symbols in offered_assets_by_chain.items():
        chain = chains_by_key[chain_key]
        chain_prices = prices_by_chain.get(chain_key)
        amount_text_by_symbol = (
            estimate_fiat_amount_by_asset_symbol(
                fiat_amount,
                fiat_currency=fiat_currency,
                asset_symbols=asset_symbols,
                chain=chain,
                prices=chain_prices,
            )
            if chain_prices is not None
            else {}
        )
        for asset_symbol in asset_symbols:
            asset = chain.find_asset(asset_symbol)
            if asset is None:
                continue
            options.append(
                PaymentOption(
                    chain=present_chain(chain),
                    asset=present_asset(asset),
                    amount=amount_text_by_symbol.get(asset.symbol),
                )
            )
    return options


async def find_chain_prices(chain_key: str, *, price_quote_service: PriceQuoteService) -> ChainPrices | None:
    """None when the chain's feeds do not answer (stale, sequencer down, unreachable)."""
    try:
        return await price_quote_service.read_chain_prices(chain_key)
    except PriceUnavailableError:
        return None


async def find_prices_by_chain(
    chain_keys: list[str], *, price_quote_service: PriceQuoteService
) -> dict[str, ChainPrices]:
    """The chains whose prices could be read, each read on its own and at the same time."""
    # A TaskGroup, not gather: a read that raises cancels the others rather than leaving them running unwatched.
    async with asyncio.TaskGroup() as price_task_group:
        price_tasks = [
            price_task_group.create_task(find_chain_prices(chain_key, price_quote_service=price_quote_service))
            for chain_key in chain_keys
        ]
    readings = [price_task.result() for price_task in price_tasks]
    return {chain_key: prices for chain_key, prices in zip(chain_keys, readings, strict=True) if prices is not None}
