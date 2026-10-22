"""backend/chains.toml plus the per-deployment overrides: ``PAY_ENABLED_CHAINS``, ``PAY_RPC_URLS_<CHAIN>``,
``PAY_PRICE_RPC_URLS_<CHAIN>``, ``PAY_FACTORY_ADDRESS_<CHAIN>``, ``PAY_TREASURY_SAFE_ADDRESS_<CHAIN>`` and ``PAY_MONERO_NODES_<CHAIN>``."""

from decimal import Decimal
from functools import lru_cache
import os
from pathlib import Path
import tomllib
from urllib.parse import urlparse

from eth_utils import is_address, to_checksum_address
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.payments.chains.types import (
    AssetConfiguration,
    ChainConfiguration,
    EvmChainConfiguration,
    MoneroChainConfiguration,
    MoneroNodeConfiguration,
    TreasuryConfiguration,
)

CHAINS_FILE = Path(__file__).resolve().parents[3] / "chains.toml"
SUPPORTED_FINALITY_TAG = "finalized"
MONERO_ASSET_SYMBOL = "XMR"
MONERO_ASSET_DECIMALS = 12
MINIMUM_MONERO_NODE_OPERATORS = 2


class ChainsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAY_", extra="ignore")

    # Comma-separated keys of backend/chains.toml.
    enabled_chains: str = Field(default="polygon")


def checksum_or_none(address: str) -> str | None:
    if not address:
        return None
    if not is_address(address):
        raise ValueError(f"{address!r} is not an address")
    return to_checksum_address(address)


def read_treasury(chain_key: str, *, table: object) -> TreasuryConfiguration:
    if not isinstance(table, dict):
        raise TypeError(f"chains.{chain_key}.treasury is not a table")
    safe_override = os.environ.get(f"PAY_TREASURY_SAFE_ADDRESS_{chain_key.upper()}", "")
    address_fields = [
        "safe_singleton_address",
        "safe_proxy_factory_address",
        "safe_fallback_handler_address",
        "safe_multi_send_call_only_address",
    ]
    treasury = TreasuryConfiguration.model_validate(
        {
            **{key: value for key, value in table.items() if key != "top_ups"},
            "treasury_safe_address": checksum_or_none(safe_override or str(table["treasury_safe_address"])),
            "allowance_module_address": checksum_or_none(str(table["allowance_module_address"])),
            **{field: to_checksum_address(str(table[field])) for field in address_fields},
            "top_up_rules": table.get("top_ups", []),
        }
    )
    if treasury.treasury_safe_address is not None and treasury.allowance_module_address is None:
        raise ValueError(f"chains.{chain_key}: a treasury Safe needs the Allowance Module, not deployed on this chain")
    return treasury


def read_assets(chain_key: str, *, table: dict[str, object]) -> list[AssetConfiguration]:
    raw_assets = table.pop("assets")
    if not isinstance(raw_assets, list):
        raise TypeError(f"chains.{chain_key}.assets is not a list")
    # An asset without its own feed limits takes the chain's (``AssetConfiguration``).
    chain_feed_limits = {
        "price_feed_heartbeat_seconds": table["price_feed_heartbeat_seconds"],
        "maximum_price_age_seconds": table["maximum_price_age_seconds"],
    }
    return [
        AssetConfiguration.model_validate(
            {
                **chain_feed_limits,
                **asset,
                "token_address": checksum_or_none(str(asset["token_address"])),
                "usd_feed": to_checksum_address(str(asset["usd_feed"])),
            }
        )
        for asset in raw_assets
    ]


def read_monero_nodes_override(chain_key: str, *, network: object) -> list[MoneroNodeConfiguration]:
    """``PAY_MONERO_NODES_<CHAIN>``: comma-separated ``url=operator`` pairs, the operator named explicitly (two hosts
    of one operator must say so, since the cross-check counts operators); no query in a URL. Mainnet takes ``https://`` only, stagenet
    ``http://`` too (some stagenet nodes answer over http only). Empty or unset: chains.toml's ``nodes``."""
    variable_name = f"PAY_MONERO_NODES_{chain_key.upper()}"
    allowed_schemes = {"https"} if network == "mainnet" else {"https", "http"}
    nodes: list[MoneroNodeConfiguration] = []
    # Errors name an entry by its position, never its text: a URL may carry credentials.
    for position, entry in enumerate(os.environ.get(variable_name, "").split(","), start=1):
        if not entry.strip():
            continue
        node_url, separator, operator = (part.strip() for part in entry.rpartition("="))
        if not separator or not operator or not node_url:
            raise ValueError(f"{variable_name}: entry {position} is not url=operator")
        parsed_url = urlparse(node_url)
        if parsed_url.scheme not in allowed_schemes or not parsed_url.hostname or parsed_url.query:
            raise ValueError(
                f"{variable_name}: entry {position} needs a {' or '.join(sorted(allowed_schemes))} URL with a host"
                " and no query"
            )
        nodes.append(MoneroNodeConfiguration(url=node_url, operator=operator))
    return nodes


def read_monero_chain(chain_key: str, *, table: dict[str, object]) -> MoneroChainConfiguration:
    nodes_override = read_monero_nodes_override(chain_key, network=table.get("network"))
    chain = MoneroChainConfiguration.model_validate(
        {
            **table,
            "key": chain_key,
            "assets": read_assets(chain_key, table=table),
            "nodes": nodes_override or table["nodes"],
            "eur_usd_feed": to_checksum_address(str(table["eur_usd_feed"])),
            "sequencer_uptime_feed": checksum_or_none(str(table.get("sequencer_uptime_feed", ""))),
        }
    )
    if len(chain.node_operators) < MINIMUM_MONERO_NODE_OPERATORS:
        raise ValueError(f"chains.{chain_key}: nodes must come from at least {MINIMUM_MONERO_NODE_OPERATORS} operators")
    if len(chain.assets) != 1 or chain.assets[0].symbol != MONERO_ASSET_SYMBOL:
        raise ValueError(f"chains.{chain_key}: the one asset of a Monero chain is {MONERO_ASSET_SYMBOL}")
    if chain.assets[0].decimals != MONERO_ASSET_DECIMALS or chain.assets[0].token_address is not None:
        raise ValueError(f"chains.{chain_key}: {MONERO_ASSET_SYMBOL} has {MONERO_ASSET_DECIMALS} decimals, no token")
    if not chain.price_rpc_urls:
        raise ValueError(f"chains.{chain_key}: a Monero chain is priced over price_rpc_urls")
    if chain.confirmations_final < 1:
        raise ValueError(f"chains.{chain_key}: confirmations_final must be at least 1")
    return chain


def read_chain(chain_key: str, *, table: dict[str, object]) -> ChainConfiguration:
    """Dispatch on ``kind`` (``evm`` when absent); each kind's class refuses the other kind's fields."""
    kind = table.pop("kind", "evm")
    if kind == "evm":
        return read_evm_chain(chain_key, table=table)
    if kind == "monero":
        return read_monero_chain(chain_key, table=table)
    raise ValueError(f"chains.{chain_key}: unknown kind {kind!r} (evm or monero)")


def read_evm_chain(chain_key: str, *, table: dict[str, object]) -> EvmChainConfiguration:
    environment_key = chain_key.upper()
    rpc_urls_override = os.environ.get(f"PAY_RPC_URLS_{environment_key}", "")
    factory_override = os.environ.get(f"PAY_FACTORY_ADDRESS_{environment_key}", "")
    # Per deployment, so a fork stack can read live prices over public endpoints (chains.toml's `price_rpc_urls`).
    price_rpc_urls_override = os.environ.get(f"PAY_PRICE_RPC_URLS_{environment_key}", "")
    treasury = read_treasury(chain_key, table=table.pop("treasury"))
    assets = read_assets(chain_key, table=table)
    chain = EvmChainConfiguration.model_validate(
        {
            **table,
            "key": chain_key,
            "assets": assets,
            "treasury": treasury,
            "rpc_urls": [url.strip() for url in rpc_urls_override.split(",") if url.strip()] or table["rpc_urls"],
            "price_rpc_urls": [url.strip() for url in price_rpc_urls_override.split(",") if url.strip()]
            or table["price_rpc_urls"],
            "factory_address": checksum_or_none(factory_override or str(table["factory_address"])),
            "eur_usd_feed": to_checksum_address(str(table["eur_usd_feed"])),
            "sequencer_uptime_feed": checksum_or_none(str(table.get("sequencer_uptime_feed", ""))),
        }
    )
    if chain.seconds_per_block <= 0 or chain.expected_finality_seconds < 0:
        raise ValueError(
            f"chains.{chain_key}: seconds_per_block must be above 0 and expected_finality_seconds not below"
        )
    if chain.finality_tag != SUPPORTED_FINALITY_TAG:
        raise ValueError(f"chains.{chain_key}: only finality_tag = {SUPPORTED_FINALITY_TAG!r} is supported")
    if sum(asset.is_native for asset in chain.assets) != 1:
        raise ValueError(f"chains.{chain_key}: exactly one asset must be the native coin")
    if len({asset.symbol.casefold() for asset in chain.assets}) != len(chain.assets):
        raise ValueError(f"chains.{chain_key}: asset symbols must differ in more than case")
    for rule in chain.treasury.top_up_rules:
        if chain.find_asset(rule.asset_symbol) is None:
            raise ValueError(f"chains.{chain_key}.treasury.top_ups names unknown asset {rule.asset_symbol}")
        if not Decimal(0) <= rule.low_amount <= rule.target_amount:
            raise ValueError(f"chains.{chain_key}.treasury.top_ups {rule.role} {rule.asset_symbol}: low above target")
    return chain


@lru_cache
def read_enabled_chains() -> list[ChainConfiguration]:
    """The enabled chains, in ``PAY_ENABLED_CHAINS`` order; an unknown key fails the start."""
    tables = tomllib.loads(CHAINS_FILE.read_text())["chains"]
    enabled_keys = [key.strip() for key in ChainsSettings().enabled_chains.split(",") if key.strip()]
    unknown_keys = [key for key in enabled_keys if key not in tables]
    if unknown_keys or not enabled_keys:
        raise ValueError(f"PAY_ENABLED_CHAINS names unknown chains {unknown_keys} (known: {sorted(tables)})")
    return [read_chain(key, table=dict(tables[key])) for key in enabled_keys]
