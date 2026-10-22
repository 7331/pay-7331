"""Invoices, the payments attributed to them, and the factory's DepositSwept events (PLAN.md sections 4 and 11).

Amounts are base units of the invoice's asset (``BaseUnits``, numeric(78,0)); fiat amounts are numeric(20,2). The
running fiat valuation of credits keeps 18 decimals so rounding can never flip a status.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import JsonValue
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base, BaseUnits, pg_enum
from app.payments.invoices.enums import InvoiceStatus, MoneroCrossCheckState
from app.payments.invoices.errors import InvoiceStateError

ADDRESS_LENGTH = 42
# The address columns an XMR invoice writes (its subaddress and its receiver's primary address; PLAN.md section 14). A
# standard address or a subaddress is 95 characters, an integrated address 106
# (https://docs.getmonero.org/public-address/standard-address/): the column takes the longest Monero address.
MONERO_ADDRESS_COLUMN_LENGTH = 106
HASH_LENGTH = 66
MONERO_HASH_LENGTH = 64


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_chain_key_open", "chain_key", postgresql_where=text("closed_at IS NULL")),
        Index("ix_invoices_created_at_id", "created_at", "id"),
        Index("ix_invoices_account_id_created_at_id", "account_id", "created_at", "id"),
        # `GET /invoices?order_id_prefix=` (2026-10-15): the collation is not C, so the unique index cannot serve LIKE.
        Index(
            "ix_invoices_account_id_order_id_pattern",
            "account_id",
            "order_id",
            postgresql_ops={"order_id": "varchar_pattern_ops"},
        ),
        # A quantity link counts its slots over these.
        Index("ix_invoices_payment_link_id", "payment_link_id", postgresql_where=text("payment_link_id IS NOT NULL")),
        # One invoice per schedule and period, however many runners try (payments/invoice_schedules; migration 0034);
        # it also lists a schedule's invoices.
        Index(
            "ux_invoices_invoice_schedule_id_period_start",
            "invoice_schedule_id",
            "invoice_schedule_period_start",
            unique=True,
            postgresql_where=text("invoice_schedule_id IS NOT NULL"),
        ),
        # The per-IP open-invoice cap counts these (PLAN.md section 9).
        Index("ix_invoices_client_ip_hash_open", "client_ip_hash", postgresql_where=text("closed_at IS NULL")),
        # And the same cap over a whole IPv6 /48 (migration 0035).
        Index(
            "ix_invoices_client_ipv6_48_hash_open",
            "client_ipv6_48_hash",
            postgresql_where=text("closed_at IS NULL AND client_ipv6_48_hash IS NOT NULL"),
        ),
        # The watcher's one candidate query a tick for automatic refund decisions waiting on a price (2026-10-12).
        Index(
            "ix_invoices_chain_key_automatic_refund_deferred",
            "chain_key",
            postgresql_where=text("automatic_refund_deferred_since IS NOT NULL"),
        ),
        # A subaddress is never reused (PLAN.md section 14); the gap guard reads the highest paid minor over this.
        Index(
            "ux_invoices_monero_wallet_id_subaddress_minor",
            "monero_wallet_id",
            "subaddress_minor",
            unique=True,
            postgresql_where=text("monero_wallet_id IS NOT NULL"),
        ),
        # An XMR invoice has both (its wallet and its minor), any other invoice neither.
        CheckConstraint(
            "(monero_wallet_id IS NULL) = (subaddress_minor IS NULL)",
            name="ck_invoices_monero_wallet_id_subaddress_minor_together",
        ),
        # The watcher reads a deposit only once its invoice has a coin; the pick sets both in one write (PLAN.md
        # section 15).
        CheckConstraint(
            "asset_symbol IS NULL OR deposit_address IS NOT NULL",
            name="ck_invoices_asset_symbol_requires_deposit_address",
        ),
        # An order id is the account's own: reusing one returns that account's invoice, never another's. One current
        # invoice per order: an expired one a refresh replaced keeps its order id and points at its successor
        # (migration 0032, 2026-10-21).
        Index(
            "ux_invoices_account_id_order_id_current",
            "account_id",
            "order_id",
            unique=True,
            postgresql_where=text("refreshed_into_invoice_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    # The first offered chain until the payer picks on an invoice offering several, then the chosen chain.
    chain_key: Mapped[str] = mapped_column(String(32))
    status: Mapped[InvoiceStatus] = mapped_column(pg_enum(InvoiceStatus, name="invoice_status"))
    partial: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    late_payment: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    sweep_blocked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # The coins of ``chain_key``: the first offered chain's until the payer picks, then the chosen chain's.
    assets: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    # Every offered chain and the coins it accepts, in the maker's order (PLAN.md section 15); fixed at creation, so
    # the pick is checked against it. ``{}`` on a row the release before migration 0027 wrote: read it through
    # ``offered_assets_by_chain_of``.
    offered_assets_by_chain: Mapped[dict[str, list[str]]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # None while awaiting_asset.
    asset_symbol: Mapped[str | None] = mapped_column(String(16))
    invoice_salt: Mapped[bytes] = mapped_column(LargeBinary(32))
    # None until the payer picks a (chain, asset) pair on an invoice offering several chains: the deposit is that
    # chain's factory's clone for the salt and destination (PLAN.md section 15). Never set to a placeholder, never shown
    # or scanned while None; read it through ``require_deposit_address`` wherever a coin is picked.
    deposit_address: Mapped[str | None] = mapped_column(String(MONERO_ADDRESS_COLUMN_LENGTH), unique=True)
    destination_address: Mapped[str] = mapped_column(String(MONERO_ADDRESS_COLUMN_LENGTH))
    # A Monero invoice's subaddress minor of its wallet (major index 1, minors from 1, never reused) and the wallet it
    # was made with; None on every EVM invoice. No ORM foreign key to the wallet's model (monero_wallets is its own
    # domain); the database's own is in migration 0023.
    subaddress_minor: Mapped[int | None] = mapped_column(Integer)
    monero_wallet_id: Mapped[int | None] = mapped_column(BigInteger)
    # The confirmations an XMR invoice's outputs count final at, taken from the receiver's setting or the chain
    # default when the deposit was allocated, and read by the watcher and the scanner sidecar after that
    # (migration 0028). Null on EVM invoices, on Monero invoices of the previous release (they stand at the
    # chain default) and while the pick is open; read it through ``monero_payment_rules.invoice_confirmations_final``.
    confirmations_final: Mapped[int | None] = mapped_column(Integer)
    # A fiat invoice has currency and amount; a crypto one has crypto_amount_base_units instead.
    fiat_currency: Mapped[str | None] = mapped_column(String(3))
    fiat_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    crypto_amount_base_units: Mapped[int | None] = mapped_column(BaseUnits)
    # What the current quote (or the crypto amount) asks in total: received plus remaining.
    amount_due_base_units: Mapped[int | None] = mapped_column(BaseUnits)
    # Finalized gross received that has been credited (PLAN.md section 4).
    received_base_units: Mapped[int] = mapped_column(BaseUnits, server_default=text("0"))
    credited_fiat_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), server_default=text("0"))
    # The part of received that came after the amount due was filled.
    excess_base_units: Mapped[int] = mapped_column(BaseUnits, server_default=text("0"))
    # Readings at `latest`, for the confirming display and native attribution.
    latest_gross_base_units: Mapped[int] = mapped_column(BaseUnits, server_default=text("0"))
    latest_balance_base_units: Mapped[int] = mapped_column(BaseUnits, server_default=text("0"))
    latest_reading_block_number: Mapped[int | None] = mapped_column(BigInteger)
    # A token invoice's native balance at `latest`: a rise is native money it did not ask for (INC-001, 2026-10-11).
    latest_native_balance_base_units: Mapped[int] = mapped_column(BaseUnits, server_default=text("0"))
    note: Mapped[str] = mapped_column(String(500), server_default=text("''"))
    # The account that made it; None for an invoice made signed out (or whose account was deleted).
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="SET NULL"))
    # Salted SHA-256 of the creating client's network (app/shared/client_network.py); never the IP itself.
    client_ip_hash: Mapped[str | None] = mapped_column(String(64))
    # Salted SHA-256 of the creating client's IPv6 /48 (app/shared/client_network.py); None for IPv4 and for every
    # invoice not made from a browser.
    client_ipv6_48_hash: Mapped[str | None] = mapped_column(String(64))
    order_id: Mapped[str | None] = mapped_column(String(128))
    # The quantity link a payer made it from (payments/payment_links, 2026-10-11); None for every other invoice. No
    # ORM foreign key: invoices sits below payment_links, so its metadata must not need that table (migration 0010
    # adds the database's own).
    payment_link_id: Mapped[str | None] = mapped_column(String(26))
    # The invoice schedule that made it and the start of the period it bills (payments/invoice_schedules, migration
    # 0034, 2026-10-21); both None for every other invoice. No ORM foreign key, as ``payment_link_id``.
    invoice_schedule_id: Mapped[str | None] = mapped_column(String(26))
    invoice_schedule_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "metadata" is reserved on declarative classes; the column keeps the API's name.
    private_metadata: Mapped[dict[str, JsonValue] | None] = mapped_column("metadata", JSONB)
    # Where the payer goes back to the shop: the back button before paying and, with no ``success_url``, the link
    # after it (2026-10-21).
    return_url: Mapped[str | None] = mapped_column(String(2048))
    # The payer's link after paying, over ``return_url`` (migration 0032, 2026-10-21).
    success_url: Mapped[str | None] = mapped_column(String(2048))
    # An account's server's per-invoice receiver of this invoice's events, signed with the account's webhook secret,
    # beside the account's webhook (migration 0032, 2026-10-21). Never on the payer's view.
    callback_url: Mapped[str | None] = mapped_column(String(2048))
    # A refresh (``refresh_expired`` on ``POST /invoices``) makes a new invoice for an expired, unpaid order: the new
    # one names the old, the old names the new. The old keeps its deposit, its order id and every rule it had, so late
    # money on it is handled as on any closed invoice (PLAN.md section 4; migration 0032, 2026-10-21).
    refreshed_from_invoice_id: Mapped[str | None] = mapped_column(String(26), ForeignKey("invoices.id"))
    refreshed_into_invoice_id: Mapped[str | None] = mapped_column(String(26))
    # The payment window and the accepted shortfall in force: the chain's, resolved when the invoice was made (the
    # maker's value, else the account's saved default for the chain, else the server's). While the payer still picks
    # among several chains, the longest of their windows and the first chain's shortfall; the pick sets the chosen
    # chain's from ``payment_terms_by_chain``.
    lifetime_seconds: Mapped[int] = mapped_column(Integer)
    accuracy_percent: Mapped[Decimal] = mapped_column(Numeric(4, 2))
    # Each offered chain's resolved terms, `{"polygon": {"lifetime_seconds": 1800, "accuracy_percent": "1.0"}}`, kept
    # only on an invoice offering several chains, so a later settings change moves no invoice (migration 0037). Null on
    # a one-chain invoice and on every invoice made before it: the two columns above then stand for any pick.
    payment_terms_by_chain: Mapped[dict[str, dict[str, JsonValue]] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    underpaid_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the invoice stops being watched every tick (expired, cancelled, swept after payment, or an underpaid
    # window that ended); the rescan still watches it (its recent tier for a day, then its old tier).
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when an automatic refund (overpay excess or stray money) could not be decided for want of a price; the
    # watcher decides it again every tick, closed or not, and clears it once decided (2026-10-12).
    automatic_refund_deferred_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def offered_assets_by_chain_of(invoice: Invoice) -> dict[str, list[str]]:
    """The invoice's offered chains and coins; a row from before migration 0027 offers its own chain and coins."""
    return invoice.offered_assets_by_chain or {invoice.chain_key: list(invoice.assets)}


def require_deposit_address(invoice: Invoice) -> str:
    """The deposit of an invoice whose coin is picked, which always has one (``ck_invoices_asset_symbol_requires_
    deposit_address``); refused for one still waiting for its pick."""
    if invoice.deposit_address is None:
        raise InvoiceStateError("the invoice has no payment address yet; pick a coin first")
    return invoice.deposit_address


class InvoicePayment(Base):
    """One attributed payment in the invoice's asset. ``transaction_hash`` and ``sender`` are None for a rise in
    gross received no transaction explains (a contract's internal transfer)."""

    __tablename__ = "invoice_payments"
    __table_args__ = (UniqueConstraint("invoice_id", "transaction_hash", "log_index"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    transaction_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH))
    # The Transfer log's index for a token; -1 for a native transaction.
    log_index: Mapped[int] = mapped_column(Integer)
    transaction_index: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    # Read once at attribution: a sender with code (a Safe) cannot personal_sign, so it is refunded by hand.
    sender_has_code: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    amount_base_units: Mapped[int] = mapped_column(BaseUnits)
    block_number: Mapped[int] = mapped_column(BigInteger)
    block_hash: Mapped[str] = mapped_column(String(HASH_LENGTH))
    # Unix seconds of the row's block, rewritten with the block at finality; a credit is valued at the quote live
    # then. None only on rows made before revision 0003 (2026-10-09).
    block_timestamp: Mapped[int | None] = mapped_column(BigInteger)
    final: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class InvoiceStrayPayment(Base):
    """Money that reached a live invoice's deposit in an asset the invoice did not ask for (INC-001, 2026-10-11), in
    that asset's base units. Shown to the payer and the account and swept to the destination with the rest; never
    credited: no accounting reads this table, so it can never change the invoice's status or amounts.
    ``transaction_hash`` and ``sender`` are None for a native rise no transaction explains (a contract sent it)."""

    __tablename__ = "invoice_stray_payments"
    __table_args__ = (
        UniqueConstraint("invoice_id", "asset_symbol", "transaction_hash", "log_index"),
        # The watcher settles finality over these.
        Index("ix_invoice_stray_payments_unfinal", "block_number", postgresql_where=text("final IS false")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    asset_symbol: Mapped[str] = mapped_column(String(16))
    transaction_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH))
    # The Transfer log's index for a token; -1 for a native transaction or an unexplained rise.
    log_index: Mapped[int] = mapped_column(Integer)
    transaction_index: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    sender_has_code: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    amount_base_units: Mapped[int] = mapped_column(BaseUnits)
    block_number: Mapped[int] = mapped_column(BigInteger)
    block_hash: Mapped[str] = mapped_column(String(HASH_LENGTH))
    block_timestamp: Mapped[int] = mapped_column(BigInteger)
    final: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class DepositSweepEvent(Base):
    """A finalized ``DepositSwept`` of this chain's factory, stored once by chain, transaction hash and log index."""

    __tablename__ = "deposit_sweep_events"
    __table_args__ = (
        UniqueConstraint("chain_key", "transaction_hash", "log_index"),
        Index("ix_deposit_sweep_events_deposit", "chain_key", "deposit_address"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chain_key: Mapped[str] = mapped_column(String(32))
    transaction_hash: Mapped[str] = mapped_column(String(HASH_LENGTH))
    log_index: Mapped[int] = mapped_column(Integer)
    deposit_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    destination_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    # None for the native coin.
    token_address: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    amount_base_units: Mapped[int] = mapped_column(BaseUnits)
    block_number: Mapped[int] = mapped_column(BigInteger)
    block_hash: Mapped[str] = mapped_column(String(HASH_LENGTH))


class MoneroPayment(Base):
    """One output that paid a Monero invoice's subaddress, as the scanner sidecar found it (PLAN.md section 14). It
    lives here, not in monero_wallets: it hangs off an invoice, and the invoices domain is the one that locks the
    invoice row and credits it. Deduplicated by output public key (the burning bug); credited from block data only."""

    __tablename__ = "monero_payments"
    __table_args__ = (
        UniqueConstraint("chain_key", "output_public_key", name="uq_monero_payments_chain_key_output_public_key"),
        Index("ix_monero_payments_invoice_id", "invoice_id"),
        # The watcher's read of what still needs a decision on one chain.
        Index("ix_monero_payments_chain_key_uncredited", "chain_key", postgresql_where=text("credited_at IS NULL")),
        # The late pass's read of credited rows no longer creditable after a reorg (migration 0026).
        Index(
            "ix_monero_payments_chain_key_credited_uncreditable",
            "chain_key",
            postgresql_where=text(
                "credited_at IS NOT NULL AND (block_height IS NULL OR cross_check_state != 'confirmed')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chain_key: Mapped[str] = mapped_column(String(32))
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"))
    txid: Mapped[str] = mapped_column(String(MONERO_HASH_LENGTH))
    output_public_key: Mapped[str] = mapped_column(String(MONERO_HASH_LENGTH))
    output_index: Mapped[int] = mapped_column(Integer)
    amount_piconero: Mapped[int] = mapped_column(BaseUnits)
    # None while the transaction is only in the pool.
    block_height: Mapped[int | None] = mapped_column(BigInteger)
    block_hash: Mapped[str | None] = mapped_column(String(MONERO_HASH_LENGTH))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    in_pool: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    double_spend_seen: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The second operator's check before crediting, and again at the final confirmation (PLAN.md section 14).
    cross_check_state: Mapped[MoneroCrossCheckState] = mapped_column(
        pg_enum(MoneroCrossCheckState, name="monero_cross_check_state"),
        server_default=MoneroCrossCheckState.PENDING.value,
    )
    cross_check_operator: Mapped[str | None] = mapped_column(String(64))
    final_cross_check_state: Mapped[MoneroCrossCheckState] = mapped_column(
        pg_enum(MoneroCrossCheckState, name="monero_cross_check_state"),
        server_default=MoneroCrossCheckState.PENDING.value,
    )


class MoneroScanCursor(Base):
    """Where the scanner sidecar's loop stands on one Monero chain (PLAN.md section 14): the last block it scanned and
    that block's hash. The sidecar resumes after it on start (from the tip when there is no row), writes it each tick
    and rewinds it when a block vanishes. Only the sidecar writes it; it lives beside ``monero_payments``, the table
    it feeds."""

    __tablename__ = "monero_scan_cursors"

    chain_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    scanned_height: Mapped[int] = mapped_column(BigInteger)
    # None while the loop follows the tip with no wallet to scan for (no block fetched, so no hash known).
    scanned_block_hash: Mapped[str | None] = mapped_column(String(MONERO_HASH_LENGTH))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
