"""Price reads and quotes other domains use."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ChainPrices(BaseModel):
    """USD per whole unit of each asset and per euro, as the chain's feeds answered."""

    model_config = ConfigDict(frozen=True)

    chain_key: str
    asset_prices_usd: dict[str, Decimal]
    eur_price_usd: Decimal

    def fiat_price_usd(self, fiat_currency: str) -> Decimal:
        return Decimal(1) if fiat_currency == "USD" else self.eur_price_usd


class QuoteValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    quote_id: int
    asset_symbol: str
    fiat_currency: str
    asset_price_usd: Decimal
    fiat_price_usd: Decimal
    eur_price_usd: Decimal
    fiat_amount: Decimal
    amount_base_units: int
    created_at: datetime
    expires_at: datetime
    superseded_at: datetime | None

    @property
    def asset_price_in_fiat(self) -> Decimal:
        """Fiat units per whole asset unit."""
        return self.asset_price_usd / self.fiat_price_usd

    def is_live_at(self, moment: datetime) -> bool:
        live_until = min(self.expires_at, self.superseded_at) if self.superseded_at else self.expires_at
        return self.created_at <= moment < live_until
