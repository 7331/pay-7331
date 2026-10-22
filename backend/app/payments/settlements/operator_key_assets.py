"""The assets an operator key look reads and the top-up sender it picks (``operator_key_maintenance_service.py``)."""

from app.infrastructure.evm_rpc_api.client import (
    encode_native_balance_call,
    encode_token_balance_call,
)
from app.infrastructure.evm_rpc_api.schemas import Multicall3Call
from app.payments.chains.types import AssetConfiguration, EvmChainConfiguration


def balance_call_of(asset: AssetConfiguration, *, holder_address: str) -> Multicall3Call:
    if asset.token_address is None:
        return encode_native_balance_call(holder_address)
    return encode_token_balance_call(asset.token_address, holder_address=holder_address)


def top_up_assets_of(chain: EvmChainConfiguration) -> list[AssetConfiguration]:
    """The native coin (gas) and every asset a top-up rule names, in the chain's order."""
    symbols = {chain.native_asset.symbol, *(rule.asset_symbol for rule in chain.treasury.top_up_rules)}
    return [asset for asset in chain.assets if asset.symbol in symbols]


def find_top_up_sender(
    address: str, *, gas_cost: int, remaining_native_by_address: dict[str, int], sending_addresses: list[str]
) -> str | None:
    """The other sending key with the most native coin left this look, when it can pay the top-up's gas."""
    candidates = [
        (remaining_native_by_address.get(candidate, 0), candidate)
        for candidate in sending_addresses
        if candidate != address
    ]
    if not candidates:
        return None
    native_balance, candidate = max(candidates)
    return candidate if native_balance >= gas_cost else None
