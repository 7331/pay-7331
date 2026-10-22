"""ChainsService: the registry of enabled chains, read once per process from backend/chains.toml."""

from app.payments.chains.errors import AssetNotSupportedError, ChainKindNotSupportedError, ChainNotEnabledError
from app.payments.chains.invoice_payment_terms import (
    DEFAULT_ACCURACY_PERCENT,
    DEFAULT_INVOICE_LIFETIME_SECONDS,
    format_accuracy_percent,
)
from app.payments.chains.schemas import ChainAsset, ChainListResponse, InvoiceChain, ListedChainResponse
from app.payments.chains.types import (
    AssetConfiguration,
    ChainConfiguration,
    EvmChainConfiguration,
    MoneroChainConfiguration,
)


class ChainsService:
    def __init__(self, *, chains: list[ChainConfiguration]) -> None:
        self.chains_by_key = {chain.key: chain for chain in chains}

    @property
    def chains(self) -> list[ChainConfiguration]:
        return list(self.chains_by_key.values())

    @property
    def evm_chains(self) -> list[EvmChainConfiguration]:
        return [chain for chain in self.chains if isinstance(chain, EvmChainConfiguration)]

    @property
    def monero_chains(self) -> list[MoneroChainConfiguration]:
        return [chain for chain in self.chains if isinstance(chain, MoneroChainConfiguration)]

    def get_chain(self, chain_key: str) -> ChainConfiguration:
        chain = self.chains_by_key.get(chain_key)
        if chain is None:
            raise ChainNotEnabledError(f"chain {chain_key!r} is not enabled here")
        return chain

    def get_evm_chain(self, chain_key: str) -> EvmChainConfiguration:
        """The chain, which must be an EVM one: the sweep, refund, treasury and recovery paths exist on no other kind."""
        chain = self.get_chain(chain_key)
        match chain:
            case EvmChainConfiguration():
                return chain
            case MoneroChainConfiguration():
                raise ChainKindNotSupportedError(
                    f"{chain_key} is a Monero chain, which has no deposit contract, sweeps, refunds, operator keys or "
                    "treasury"
                )
            case _:
                raise ChainKindNotSupportedError(f"{chain_key} is not an EVM chain")

    def get_monero_chain(self, chain_key: str) -> MoneroChainConfiguration:
        """The chain, which must be a Monero one: wallets, subaddresses and view keys exist on no other kind."""
        chain = self.get_chain(chain_key)
        match chain:
            case MoneroChainConfiguration():
                return chain
            case _:
                raise ChainKindNotSupportedError(f"{chain_key} is not a Monero chain")

    def get_asset(self, chain_key: str, *, symbol: str) -> AssetConfiguration:
        asset = self.get_chain(chain_key).find_asset(symbol)
        if asset is None:
            raise AssetNotSupportedError(f"asset {symbol!r} is not configured on {chain_key}")
        return asset

    def present_chain_list_response(self) -> ChainListResponse:
        """The chains a new invoice can go to (``takes_invoices``: an EVM chain with a deposit factory deployed, and
        every Monero chain, for accounts with a Monero wallet on it), in ``PAY_ENABLED_CHAINS`` order, so the first EVM
        one is the one `/`'s public invoices use (InvoiceService.find_first_invoiceable_chain)."""
        return ChainListResponse(chains=[present_listed_chain(chain) for chain in self.chains if chain.takes_invoices])


def present_asset(asset: AssetConfiguration) -> ChainAsset:
    return ChainAsset(symbol=asset.symbol, name=asset.name, decimals=asset.decimals, token_address=asset.token_address)


def present_chain(chain: ChainConfiguration) -> InvoiceChain:
    return InvoiceChain(
        key=chain.key,
        name=chain.name,
        kind=chain.kind,
        chain_id=chain.evm_chain_id_or_none,
        explorer_url=chain.explorer_url,
        expected_finality_seconds=chain.payer_finality_seconds,
        confirmations_final=chain.confirmations_final if isinstance(chain, MoneroChainConfiguration) else None,
    )


def present_listed_chain(chain: ChainConfiguration) -> ListedChainResponse:
    invoice_chain = present_chain(chain)
    return ListedChainResponse(
        key=invoice_chain.key,
        name=invoice_chain.name,
        kind=invoice_chain.kind,
        chain_id=invoice_chain.chain_id,
        explorer_url=invoice_chain.explorer_url,
        expected_finality_seconds=invoice_chain.expected_finality_seconds,
        confirmations_final=invoice_chain.confirmations_final,
        assets=[present_asset(asset) for asset in chain.assets],
        default_accepted_assets=chain.default_accepted_asset_symbols,
        default_invoice_lifetime_seconds=DEFAULT_INVOICE_LIFETIME_SECONDS,
        default_accuracy_percent=format_accuracy_percent(DEFAULT_ACCURACY_PERCENT),
    )
