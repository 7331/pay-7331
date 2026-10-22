"""Payment URIs (PLAN.md sections 10 and 14), split out of ``presenters.py``: EIP-681 on an EVM chain, wallet2's
``monero:`` URI on a Monero chain."""

from urllib.parse import quote

from app.payments.chains.types import (
    AssetConfiguration,
    ChainConfiguration,
    EvmChainConfiguration,
    MoneroChainConfiguration,
)
from app.shared.amounts import format_base_units, to_scientific_text


def build_monero_payment_uri(*, subaddress: str, asset: AssetConfiguration, amount_piconero: int, note: str) -> str:
    """``monero:<subaddress>?tx_amount=<XMR>&tx_description=<note>`` (PLAN.md section 14): the amount in decimal XMR,
    trailing zeros trimmed, the note URL-encoded, each left out when empty, as wallet2's ``make_uri`` writes it
    (https://github.com/monero-project/monero/wiki/URI-Formatting; src/wallet/wallet2.cpp ``make_uri``)."""
    query_parameters: list[str] = []
    if amount_piconero > 0:
        query_parameters.append(f"tx_amount={format_base_units(amount_piconero, decimals=asset.decimals)}")
    if note:
        query_parameters.append(f"tx_description={quote(note, safe='')}")
    return f"monero:{subaddress}" + (f"?{'&'.join(query_parameters)}" if query_parameters else "")


def build_payment_uri(
    *, chain: ChainConfiguration, asset: AssetConfiguration, deposit_address: str, amount: int, note: str
) -> str:
    """EIP-681 (PLAN.md section 10): a native ``value`` or a token ``transfer``; a Monero URI on a Monero chain."""
    if isinstance(chain, MoneroChainConfiguration):
        return build_monero_payment_uri(subaddress=deposit_address, asset=asset, amount_piconero=amount, note=note)
    if not isinstance(chain, EvmChainConfiguration):
        raise NotImplementedError(f"no payment URI for a {chain.kind} chain")
    if asset.token_address is None:
        return f"ethereum:{deposit_address}@{chain.chain_id}?value={to_scientific_text(amount)}"
    return f"ethereum:{asset.token_address}@{chain.chain_id}/transfer?address={deposit_address}&uint256={amount}"
