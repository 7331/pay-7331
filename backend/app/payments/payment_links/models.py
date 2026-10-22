"""Quantity links: one account's link that up to ``quantity`` payers pay, each with
an invoice of its own (``invoices.payment_link_id``)."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base

# A Monero receiver address column (PLAN.md section 14): a standard address or a subaddress is 95 characters, an
# integrated address 106 (https://docs.getmonero.org/public-address/standard-address/), so it takes the longest.
MONERO_ADDRESS_COLUMN_LENGTH = 106


def offered_assets_by_chain_of(payment_link: "PaymentLink") -> dict[str, list[str]]:
    return payment_link.offered_assets_by_chain or {payment_link.chain_key: list(payment_link.assets)}


class PaymentLink(Base):
    __tablename__ = "payment_links"
    __table_args__ = (
        Index("ix_payment_links_account_id_created_at_id", "account_id", "created_at", "id"),
        CheckConstraint("quantity BETWEEN 1 AND 1000", name="ck_payment_links_quantity"),
    )

    # `lnk_` and 22 base58 characters (app/shared/identifiers.py).
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    # None once its account is deleted: the link then takes no more payers.
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="SET NULL"))
    # The first offered chain and its coins: what a client that knows one chain sees (PLAN.md section 15).
    chain_key: Mapped[str] = mapped_column(String(32))
    assets: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    # Every offered chain and the coins it accepts, in the maker's order; fixed when the link is made. ``{}`` on a row
    # from before migration 0027: read it through ``offered_assets_by_chain_of``.
    offered_assets_by_chain: Mapped[dict[str, list[str]]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # USD or EUR only: every payer is asked the same money.
    fiat_currency: Mapped[str] = mapped_column(String(3))
    fiat_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    # Fixed when the link is made: a later payout address change does not move its invoices.
    destination_address: Mapped[str] = mapped_column(String(MONERO_ADDRESS_COLUMN_LENGTH))
    note: Mapped[str] = mapped_column(String(500), server_default=text("''"))
    return_url: Mapped[str | None] = mapped_column(String(2048))
    # 1 to 1000 payers.
    quantity: Mapped[int] = mapped_column(Integer)
    # Each payer's invoice lives this long and asks for payment within this accuracy.
    lifetime_seconds: Mapped[int] = mapped_column(Integer)
    accuracy_percent: Mapped[Decimal] = mapped_column(Numeric(4, 2))
    # Set when the maker closes it; no payer gets a new invoice after.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
