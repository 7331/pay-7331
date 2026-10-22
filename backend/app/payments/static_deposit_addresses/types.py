"""What the static deposit address services take and return, the watcher's scan included (PLAN.md section 17)."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class StaticDepositAddressTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    received_usd: Decimal
    payment_count: int
    last_payment_at: datetime | None


class ScannedStaticDeposit(BaseModel):
    """One address as the watcher's scan pages it."""

    model_config = ConfigDict(frozen=True)

    static_deposit_address_id: str
    deposit_address: str
    created_at: datetime
    last_scanned_block_number: int | None
    # The tokens its sweeps leave out: money in them is not a sweep that never got asked.
    skipped_sweep_token_symbols: tuple[str, ...] = ()


class StaticDepositReading(BaseModel):
    """One address's balance in every configured coin at the finalized block (a failed read is absent, so that coin
    is left for the next pass)."""

    model_config = ConfigDict(frozen=True)

    static_deposit_address_id: str
    balances_by_symbol: dict[str, int]


class StaticDepositRise(BaseModel):
    """New money in one coin of one address: its gross over what its payment rows explain."""

    model_config = ConfigDict(frozen=True)

    static_deposit_address_id: str
    deposit_address: str
    asset_symbol: str
    token_address: str | None
    amount_base_units: int
    # Where the Transfer logs explaining it start: after the last scanned block.
    last_scanned_block_number: int | None


class StaticDepositRiseSearch(BaseModel):
    """A slice's new money, and the (address id, coin) pairs whose gross fell below what their rows hold (nothing
    recorded for those; the scan logs a pair when it enters that state, not every pass)."""

    model_config = ConfigDict(frozen=True)

    rises: list[StaticDepositRise]
    gross_below_recorded_keys: frozenset[tuple[str, str]]


class StaticDepositTransfer(BaseModel):
    """One Transfer log into a static address, read at or below the finalized block."""

    model_config = ConfigDict(frozen=True)

    deposit_address: str
    token_address: str
    transaction_hash: str
    log_index: int
    sender: str
    amount_base_units: int
    block_number: int
