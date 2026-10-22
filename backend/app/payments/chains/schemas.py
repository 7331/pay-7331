"""``GET /api/v1/chains`` and the chain and asset objects the invoice resource embeds."""

from app.payments.chains.enums import ChainKind
from app.shared.schemas import ApiResponse


class ChainAsset(ApiResponse):
    symbol: str
    name: str
    decimals: int
    token_address: str | None


class InvoiceChain(ApiResponse):
    """A chain as an invoice, a quantity link, a payment option or a customer address names it."""

    key: str
    name: str
    kind: ChainKind
    # Null on a Monero chain, which has none.
    chain_id: int | None
    explorer_url: str
    # How long a payment on this chain usually takes to count: the payer's confirming screen says "usually within ..."
    # from it. Polygon 5, Arbitrum One 900, Monero its confirmations times its block time.
    expected_finality_seconds: int
    # A Monero chain's confirmations at which money moves before an account's own setting; null on an EVM chain, which
    # waits for its `finalized` tag instead.
    confirmations_final: int | None


class ListedChainResponse(InvoiceChain):
    """One entry of ``GET /chains``: the chain, its coins and the ones a new invoice starts with ticked."""

    assets: list[ChainAsset]
    # The coins a new invoice starts with ticked (signed out, or an account that saved none).
    default_accepted_assets: list[str]


class ChainListResponse(ApiResponse):
    chains: list[ListedChainResponse]
