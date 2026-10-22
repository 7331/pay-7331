"""Quotes: a fiat amount converted to asset base units at Chainlink prices, live until ``expires_at``."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Identity, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base, BaseUnits

PRICE_PRECISION = Numeric(38, 18)


class PriceQuote(Base):
    __tablename__ = "price_quotes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(26), index=True)
    chain_key: Mapped[str] = mapped_column(String(32))
    asset_symbol: Mapped[str] = mapped_column(String(16))
    fiat_currency: Mapped[str] = mapped_column(String(3))
    # USD per whole asset unit, USD per fiat unit (1 for USD) and USD per euro, as the feeds answered.
    asset_price_usd: Mapped[Decimal] = mapped_column(PRICE_PRECISION)
    fiat_price_usd: Mapped[Decimal] = mapped_column(PRICE_PRECISION)
    eur_price_usd: Mapped[Decimal] = mapped_column(PRICE_PRECISION)
    # The fiat amount the quote converted (the remainder for a fresh quote) and its asset amount, rounded up.
    fiat_amount: Mapped[Decimal] = mapped_column(PRICE_PRECISION)
    amount_base_units: Mapped[int] = mapped_column(BaseUnits)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Set when a newer quote for the same invoice replaces it (one live quote at a time).
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
