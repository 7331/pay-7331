"""Static deposit addresses and the money each received (PLAN.md section 17; migration 0033).

Amounts are base units of the payment's asset (``BaseUnits``, numeric(78,0)); USD values are numeric(20,2), the
price kept with 18 decimals.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base, BaseUnits
from app.shared.identifiers import generate_public_id

ADDRESS_LENGTH = 42
HASH_LENGTH = 66
CUSTOMER_REFERENCE_LENGTH = 128


class StaticDepositAddress(Base):
    """One account's permanent deposit for one customer on one chain: its destination fixed at creation and baked
    into the address, so a later payout address change never moves it."""

    __tablename__ = "static_deposit_addresses"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "chain_key",
            "customer_reference",
            name="uq_static_deposit_addresses_account_chain_reference",
        ),
        # The prefix list (the collation is not C, so the unique index cannot serve an anchored LIKE).
        Index(
            "ix_static_deposit_addresses_account_id_reference_pattern",
            "account_id",
            "customer_reference",
            postgresql_ops={"customer_reference": "varchar_pattern_ops"},
        ),
        Index("ix_static_deposit_addresses_account_id_created_at_id", "account_id", "created_at", "id"),
        # The watcher's scan pages a chain's addresses in creation order.
        Index("ix_static_deposit_addresses_chain_key_created_at_id", "chain_key", "created_at", "id"),
        # The scan's hot tier: addresses that received money lately.
        Index("ix_static_deposit_addresses_chain_key_last_received_at", "chain_key", "last_received_at"),
        # The watcher's minute check of blocked sweeps (reconcile's outgoing operations).
        Index(
            "ix_static_deposit_addresses_chain_key_sweep_blocked",
            "chain_key",
            postgresql_where=text("sweep_blocked"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    # None only once its account is deleted: still scanned and swept, but no events.
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="SET NULL"))
    chain_key: Mapped[str] = mapped_column(String(32))
    customer_reference: Mapped[str] = mapped_column(String(CUSTOMER_REFERENCE_LENGTH))
    # The coins it credits, in the chain's order; any other configured coin is wrong-coin money.
    assets: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    deposit_salt: Mapped[bytes] = mapped_column(LargeBinary(32))
    deposit_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH), unique=True)
    destination_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    # A sweep of it failed (not a frozen key's): the money waits for ``sweep-static-deposit-address``.
    sweep_blocked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Configured tokens every sweep of it leaves out (one whose transfer reverts, e.g. frozen by its issuer): set by
    # ``sweep-static-deposit-address --skip-token``, emptied by its ``--clear-skipped-tokens``.
    skipped_sweep_token_symbols: Mapped[list[str]] = mapped_column(ARRAY(String(16)), server_default=text("'{}'"))
    # The finalized block the last applied scan read it at; the next scan's Transfer logs start after it.
    last_scanned_block_number: Mapped[int | None] = mapped_column(BigInteger)
    # When a scan last recorded money on it; with ``created_at`` it puts the address in the scan's hot tier.
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


STATIC_DEPOSIT_PAYMENT_PUBLIC_ID_PREFIX = "sdp_"


class StaticDepositPayment(Base):
    """One rise in what an address received in one coin, or one Transfer log of it. ``transaction_hash`` and
    ``sender`` are None for money no Transfer log explained (a native payment, a contract's internal transfer, a log
    read that failed). ``credited`` rows are the account's payments; the others are wrong-coin money, swept all the
    same."""

    __tablename__ = "static_deposit_payments"
    __table_args__ = (
        UniqueConstraint(
            "static_deposit_address_id",
            "transaction_hash",
            "log_index",
            name="uq_static_deposit_payments_address_transaction_log",
        ),
        CheckConstraint("credited = (uncredited_reason IS NULL)", name="ck_static_deposit_payments_credited_or_reason"),
        Index("ix_static_deposit_payments_static_deposit_address_id_id", "static_deposit_address_id", "id"),
        Index("ux_static_deposit_payments_public_id", "public_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The payment's id on the wire (`sdp_...`, migration 0038); the integer key stays internal.
    public_id: Mapped[str] = mapped_column(
        String(26),
        default=lambda: generate_public_id(STATIC_DEPOSIT_PAYMENT_PUBLIC_ID_PREFIX),
        server_default=text(f"generate_public_id('{STATIC_DEPOSIT_PAYMENT_PUBLIC_ID_PREFIX}')"),
    )
    static_deposit_address_id: Mapped[str] = mapped_column(ForeignKey("static_deposit_addresses.id"))
    asset_symbol: Mapped[str] = mapped_column(String(16))
    amount_base_units: Mapped[int] = mapped_column(BaseUnits)
    # USD per whole coin when it was recorded (pegged stablecoins at the peg rule); None when uncredited money found
    # no price.
    asset_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    credited: Mapped[bool] = mapped_column(Boolean)
    # An ``UncreditedReason`` value; None exactly when credited.
    uncredited_reason: Mapped[str | None] = mapped_column(String(32))
    transaction_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH))
    # The Transfer log's index; -1 for a row no log explains.
    log_index: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    # The Transfer's block, or the finalized block the scan read for a row no log explains.
    block_number: Mapped[int] = mapped_column(BigInteger)
