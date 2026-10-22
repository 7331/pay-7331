"""A chain's public facts as read from backend/chains.toml; every domain reads chains through these."""

from decimal import ROUND_CEILING, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.payments.chains.enums import ChainKind

# The coins a new invoice accepts until its maker ticks others or an account saves its own, where the chain lists them
# (the one copy of this list; the frontend keeps none).
DEFAULT_ACCEPTED_ASSET_SYMBOLS = ("POL", "ETH", "USDC", "USDT")


class AssetConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    name: str
    decimals: int
    # Places a quote is rounded up to, so the amount stays short enough to type.
    quote_decimals: int
    # None for the chain's native coin.
    token_address: str | None
    usd_feed: str
    # The ``usd_feed``'s own Chainlink heartbeat and the oldest answer of it a price may use. chains.toml may leave
    # them out: the loader (configuration.py) fills in the chain's ``price_feed_heartbeat_seconds`` and
    # ``maximum_price_age_seconds``. Set only for a slow feed (XMR/USD: 24 h or 1% deviation, 2026-10-14).
    price_feed_heartbeat_seconds: int
    maximum_price_age_seconds: int
    # A USD stablecoin priced at exactly 1 USD while its feed stays near the peg (``price_quotes``).
    usd_peg: bool = False
    # Below this a deposit is not worth a sweep's gas: no invoice is made for less, and a deposit that was swept once
    # is not swept again for less (dust griefing, 2026-10-09).
    minimum_sweep_amount: Decimal

    @property
    def is_native(self) -> bool:
        return self.token_address is None

    @property
    def minimum_sweep_base_units(self) -> int:
        return self.base_units_of(self.minimum_sweep_amount)

    def base_units_of(self, amount: Decimal) -> int:
        """An amount in token units (as chains.toml writes them) in base units."""
        return int(amount.scaleb(self.decimals))


class TopUpRule(BaseModel):
    """How the treasury Safe keeps one operator key role funded in one asset (settlements'
    ``operator_key_maintenance_service.py``): a key below ``low_amount`` pulls itself back to ``target_amount``
    through its Allowance Module allowance of ``daily_allowance_amount`` a day. Amounts in token units."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # "refund": the refund key (float and refund gas); "sweeper": each sweeper key (gas only).
    role: Literal["refund", "sweeper"]
    asset_symbol: str
    low_amount: Decimal
    target_amount: Decimal
    daily_allowance_amount: Decimal


class TreasuryConfiguration(BaseModel):
    """The treasury Safe that funds the operator keys through Safe's Allowance Module (PLAN.md section 5, treasury).
    The Safe v1.4.1 deployment addresses are what ``treasury create-safe`` and ``treasury setup-transactions`` use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # None: treasury off (no top-ups, no treasury returns).
    treasury_safe_address: str | None
    # None where the module is not deployed (Amoy): the treasury cannot be turned on there.
    allowance_module_address: str | None
    safe_singleton_address: str
    safe_proxy_factory_address: str
    safe_fallback_handler_address: str
    safe_multi_send_call_only_address: str
    top_up_rules: list[TopUpRule]

    @property
    def is_enabled(self) -> bool:
        return self.treasury_safe_address is not None and self.allowance_module_address is not None

    def top_up_rules_of_role(self, role: str) -> list[TopUpRule]:
        return [rule for rule in self.top_up_rules if rule.role == role]


# Monero's target block time: 2 minutes.
MONERO_SECONDS_PER_BLOCK = 120


class ChainConfiguration(BaseModel):
    """What every chain kind has: its name, its assets and how they are priced (a Chainlink feed over an EVM RPC, as
    PLAN.md section 14 prices XMR). ``EvmChainConfiguration`` and ``MoneroChainConfiguration`` add the rest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    name: str
    explorer_url: str
    # The watcher tick (EVM) or the scanner poll (Monero).
    poll_interval_seconds: float
    # The EUR/USD feed's and, unless an asset sets its own, each asset feed's limits (``AssetConfiguration``).
    maximum_price_age_seconds: int
    # The feeds' Chainlink heartbeat: an answer older than this plus a grace minute is a stalled feed for reconcile
    # and the operator alerts (2026-10-12), well before `maximum_price_age_seconds` refuses quotes.
    price_feed_heartbeat_seconds: int
    # Empty: the chain's own endpoints (EVM only; a Monero chain always names its price endpoints).
    price_rpc_urls: list[str]
    price_chain_id: int
    eur_usd_feed: str
    # The rollup's L2 Sequencer Uptime Feed (Chainlink, https://docs.chain.link/data-feeds/l2-sequencer-feeds): while it
    # answers 1 (down), or for an hour after it came back, no price of this chain is used (``chainlink_api``). None
    # where the chain has none (Polygon, Monero, and a fork that reads a rollup's feeds on its own: the fork's own
    # entry names it).
    sequencer_uptime_feed: str | None = None
    assets: list[AssetConfiguration]

    @property
    def kind(self) -> ChainKind:
        raise NotImplementedError

    @property
    def payer_finality_seconds(self) -> int:
        """How long a payment usually takes to count, as the payer's confirming screen says it."""
        raise NotImplementedError

    @property
    def takes_invoices(self) -> bool:
        """Whether a new invoice can be made on this chain right now."""
        raise NotImplementedError

    @property
    def evm_chain_id_or_none(self) -> int | None:
        """The chain id the API shows; None on a chain that has none."""
        raise NotImplementedError

    def find_asset(self, symbol: str) -> AssetConfiguration | None:
        """Case-insensitive, so a request may say ``usdc.e`` or ``USDC.E`` for ``USDC.e``; configuration refuses two
        symbols that differ only in case."""
        folded_symbol = symbol.casefold()
        return next((asset for asset in self.assets if asset.symbol.casefold() == folded_symbol), None)

    @property
    def default_accepted_asset_symbols(self) -> list[str]:
        """``DEFAULT_ACCEPTED_ASSET_SYMBOLS`` this chain lists, in its order; the first asset when it lists none."""
        listed_defaults = [asset.symbol for asset in self.assets if asset.symbol in DEFAULT_ACCEPTED_ASSET_SYMBOLS]
        return listed_defaults or [self.assets[0].symbol]

    def order_accepted_asset_symbols(self, asset_symbols: list[str]) -> list[str]:
        """The chain's symbols among ``asset_symbols`` (case-insensitive), in the chain's order, each once; unknown
        ones dropped."""
        folded_symbols = {symbol.casefold() for symbol in asset_symbols}
        return [asset.symbol for asset in self.assets if asset.symbol.casefold() in folded_symbols]

    @property
    def native_asset(self) -> AssetConfiguration:
        return next(asset for asset in self.assets if asset.is_native)


class EvmChainConfiguration(ChainConfiguration):
    chain_id: int
    rpc_urls: list[str]
    finality_tag: str
    # The rescan's two tiers (chain_watcher/rescan_service.py, 2026-10-13): deposits whose invoice is younger than
    # `recent_deposit_window_seconds` or not closed yet are reread every `rescan_interval_seconds`, every other one
    # every `old_deposit_rescan_interval_seconds`.
    rescan_interval_seconds: float
    recent_deposit_window_seconds: float
    old_deposit_rescan_interval_seconds: float
    # The static deposit scan's two tiers (chain_watcher/static_deposit_scan_service.py, PLAN.md section 17): addresses
    # made or paid within `static_deposit_hot_window_seconds` are read every `static_deposit_hot_scan_interval_seconds`,
    # every other one every `static_deposit_cold_scan_interval_seconds`. Only the forks set them, to see a cold pass.
    static_deposit_hot_scan_interval_seconds: float = 60
    static_deposit_hot_window_seconds: float = 86400
    static_deposit_cold_scan_interval_seconds: float = 1200
    # Gwei as a decimal, since a rollup's fees are fractions of one (Arbitrum One's base fee is 0.02 gwei, 2026-10-14).
    minimum_priority_fee_gwei: Decimal
    fee_bump_after_seconds: int
    # No attempt is signed above this fee cap; bumps stop below it.
    maximum_fee_per_gas_gwei: Decimal
    # The average time between blocks. Every "about N blocks" threshold of the watcher is written in seconds and turned
    # into blocks with this (PLAN.md section 15): Polygon 2, Arbitrum One 0.25.
    seconds_per_block: Decimal
    # How long `finalized` usually trails the moment a payment is mined: Polygon 2 to 5 s, Arbitrum One about 15 minutes
    # (the batch's post plus Ethereum finality). Only the payer's confirming screen and the alert ages read it; money
    # moves on the `finalized` tag itself.
    expected_finality_seconds: int
    # None until the factory is deployed; invoices cannot be made on such a chain.
    factory_address: str | None
    treasury: TreasuryConfiguration

    @property
    def kind(self) -> ChainKind:
        return ChainKind.EVM

    @property
    def payer_finality_seconds(self) -> int:
        return self.expected_finality_seconds

    @property
    def minimum_priority_fee_per_gas_wei(self) -> int:
        return int(self.minimum_priority_fee_gwei.scaleb(9))

    @property
    def maximum_fee_per_gas_wei(self) -> int:
        return int(self.maximum_fee_per_gas_gwei.scaleb(9))

    def blocks_in_seconds(self, seconds: float) -> int:
        """How many of this chain's blocks span ``seconds``, rounded up, at least 1."""
        return max(1, int((Decimal(str(seconds)) / self.seconds_per_block).to_integral_value(ROUND_CEILING)))

    @property
    def takes_invoices(self) -> bool:
        return self.factory_address is not None

    @property
    def evm_chain_id_or_none(self) -> int | None:
        return self.chain_id

    @property
    def token_addresses(self) -> list[str]:
        return [asset.token_address for asset in self.assets if asset.token_address is not None]


class MoneroNodeConfiguration(BaseModel):
    """One restricted-RPC node. Two nodes of one ``operator`` count once toward the cross-check (PLAN.md section 14)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    operator: str


class MoneroChainConfiguration(ChainConfiguration):
    """A Monero chain (PLAN.md section 14): the receiver's own wallet is the destination, so there is no factory, no
    sweep, no operator key and no treasury, and the server holds view keys only."""

    network: Literal["mainnet", "stagenet"]
    nodes: list[MoneroNodeConfiguration]
    # The enabled EVM chain whose RPC client reads the feeds (``polygon``); its chain id must equal
    # ``price_chain_id``. When it is not enabled, ``price_rpc_urls`` get their own client. Never a fork: an anvil
    # fork shares chain id 137 but its feeds stand still.
    price_chain_key: str
    # Confirmations at which money moves: mainnet 10 (the consensus spend lock), stagenet 2 for the end-to-end run.
    confirmations_final: int
    # Late payments (PLAN.md section 14): the sidecar keeps watching a replaced wallet, and the watcher marks money that
    # reaches a closed invoice late, while the invoice closed less than this long ago.
    recent_deposit_window_seconds: float = 86400

    @property
    def kind(self) -> ChainKind:
        return ChainKind.MONERO

    @property
    def payer_finality_seconds(self) -> int:
        return self.confirmations_final * MONERO_SECONDS_PER_BLOCK

    @property
    def takes_invoices(self) -> bool:
        # Always: an account with a Monero wallet on the chain makes XMR invoices and links (PLAN.md section 14);
        # signed out, or with no wallet, creation refuses (`chain_kind_not_supported`, `monero_wallet_missing`).
        return True

    @property
    def evm_chain_id_or_none(self) -> int | None:
        return None

    @property
    def node_operators(self) -> set[str]:
        return {node.operator for node in self.nodes}
