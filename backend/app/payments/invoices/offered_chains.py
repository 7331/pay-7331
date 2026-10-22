"""Which chains and coins an invoice or a quantity link offers (PLAN.md section 15), resolved from a request.

One chain keeps every rule of before. Several (EVM only: a Monero invoice needs its receiver's view key and is made on
its own) offer the same coin list filtered to each chain: a chain the maker named that has none of the coins is
refused; a chain the gateway filled in for them (``defaulted_chains``) that has none, or that cannot be reached, is
left out.
"""

from app.payments.chains.errors import AssetNotSupportedError
from app.payments.chains.service import ChainsService
from app.payments.chains.types import AssetConfiguration, ChainConfiguration, MoneroChainConfiguration
from app.payments.invoices.enums import FiatCurrency
from app.payments.invoices.types import AccountInvoiceOffer
from app.shared.errors import InvalidInputError

FIAT_CURRENCIES = frozenset(fiat_currency.value for fiat_currency in FiatCurrency)


def read_requested_chain_keys(
    *, chain: str | None, chains: list[str] | None, assets_by_chain: dict[str, list[str]] | None = None
) -> list[str]:
    """``chain`` (one chain, as before) or ``chains`` (one or more), never both; with neither, the chains
    ``assets_by_chain`` names (the maker's exact per-chain ticks)."""
    if chain is not None and chains is not None:
        raise InvalidInputError("chain: send either chain or chains")
    if chain is not None:
        return [chain]
    if chains is not None:
        return list(dict.fromkeys(chains))
    if assets_by_chain:
        return list(assets_by_chain)
    raise InvalidInputError("chain: send either chain or chains")


def resolve_offered_assets(
    chains_service: ChainsService,
    *,
    chain_keys: list[str],
    asset_symbols: list[str],
    assets_by_chain: dict[str, list[str]] | None,
    defaulted_chains: bool,
) -> dict[str, list[AssetConfiguration]]:
    """Each offered chain's coins, in the order of ``chain_keys``; the first chain is the one an old client sees until
    the payer picks. A chain named in ``assets_by_chain`` takes exactly that list (each coin must exist there, at
    least one); any other takes the coins of ``asset_symbols`` it lists (case-insensitive, a repeat dropped, first
    mention kept)."""
    if not asset_symbols and not assets_by_chain:
        raise InvalidInputError("assets: tick at least one coin")
    chains = [chains_service.get_chain(chain_key) for chain_key in dict.fromkeys(chain_keys)]
    explicit_by_chain = assets_by_chain or {}
    check_offered_chains_combination(chains, explicit_by_chain=explicit_by_chain, asset_symbols=asset_symbols)
    offered: dict[str, list[AssetConfiguration]] = {}
    for chain in chains:
        chain_assets = select_offered_chain_assets(
            chains_service,
            chain=chain,
            explicit_by_chain=explicit_by_chain,
            asset_symbols=asset_symbols,
            is_only_chain=len(chains) == 1,
        )
        if chain_assets:
            offered[chain.key] = list(chain_assets.values())
        elif not defaulted_chains:
            raise InvalidInputError(f"assets: none of these coins is accepted on {chain.key}")
    if not offered:
        raise InvalidInputError("assets: tick at least one coin")
    return offered


def check_offered_chains_combination(
    chains: list[ChainConfiguration], *, explicit_by_chain: dict[str, list[str]], asset_symbols: list[str]
) -> None:
    """Refuses Monero beside another chain, an ``assets_by_chain`` key that is not an offered chain, and (with several
    chains) a shared coin that none of the chains without their own list has."""
    if len(chains) > 1 and any(isinstance(chain, MoneroChainConfiguration) for chain in chains):
        raise InvalidInputError("chains: a Monero invoice is made on its own, not beside other chains")
    offered_chain_keys = {chain.key for chain in chains}
    for explicit_chain_key in explicit_by_chain:
        if explicit_chain_key not in offered_chain_keys:
            raise InvalidInputError(f"assets_by_chain: {explicit_chain_key} is not one of the offered chains")
    shared_chains = [chain for chain in chains if chain.key not in explicit_by_chain]
    for symbol in asset_symbols:
        if len(chains) > 1 and shared_chains and not any(chain.find_asset(symbol) for chain in shared_chains):
            raise AssetNotSupportedError(f"asset {symbol!r} is not configured on any of the offered chains")


def select_offered_chain_assets(
    chains_service: ChainsService,
    *,
    chain: ChainConfiguration,
    explicit_by_chain: dict[str, list[str]],
    asset_symbols: list[str],
    is_only_chain: bool,
) -> dict[str, AssetConfiguration]:
    """One chain's coins by symbol: its own ``assets_by_chain`` list (each must exist, at least one), else on the only
    chain every shared coin (each must exist), else the shared coins this chain has."""
    if chain.key in explicit_by_chain:
        chain_assets = {
            asset.symbol: asset
            for asset in (chains_service.get_asset(chain.key, symbol=symbol) for symbol in explicit_by_chain[chain.key])
        }
        if not chain_assets:
            raise InvalidInputError(f"assets_by_chain.{chain.key}: tick at least one coin")
        return chain_assets
    if is_only_chain:
        return {
            asset.symbol: asset
            for asset in (chains_service.get_asset(chain.key, symbol=symbol) for symbol in asset_symbols)
        }
    return {asset.symbol: asset for symbol in asset_symbols if (asset := chain.find_asset(symbol)) is not None}


def list_every_asset_symbol(chains_service: ChainsService, *, chain_keys: list[str]) -> list[str]:
    """Every coin of the chains, each symbol once, in chain order: what an invoice made with no ``assets`` accepts."""
    return list(
        dict.fromkeys(asset.symbol for chain_key in chain_keys for asset in chains_service.get_chain(chain_key).assets)
    )


def resolve_account_invoice_offer(
    *,
    invoiceable_evm_chain_keys: list[str],
    chain: str | None,
    chains: list[str] | None,
    assets: list[str] | None,
    assets_by_chain: dict[str, list[str]] | None,
    currency: str,
    accepted_asset_symbols_by_chain: dict[str, list[str]],
) -> AccountInvoiceOffer:
    """What an account's invoice offers: with no network field every enabled EVM chain with a
    factory (``defaulted_chains``); with no coin field a fiat amount takes the account's saved coins on each chain and
    a coin amount its own coin, so ``amount`` and ``currency`` alone make an invoice. ``currency`` is upper case."""
    defaulted_chains = chain is None and chains is None and assets_by_chain is None
    chain_keys = (
        invoiceable_evm_chain_keys
        if defaulted_chains
        else read_requested_chain_keys(chain=chain, chains=chains, assets_by_chain=assets_by_chain)
    )
    offered_assets = assets or []
    offered_assets_by_chain = assets_by_chain
    if assets is None and assets_by_chain is None:
        if currency in FIAT_CURRENCIES:
            offered_assets_by_chain = {
                chain_key: accepted_asset_symbols_by_chain[chain_key]
                for chain_key in chain_keys
                if chain_key in accepted_asset_symbols_by_chain
            }
        else:
            offered_assets = [currency]
    return AccountInvoiceOffer(
        chain_keys=chain_keys,
        defaulted_chains=defaulted_chains,
        assets=offered_assets,
        assets_by_chain=offered_assets_by_chain,
    )
