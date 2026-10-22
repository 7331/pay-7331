"""Outgoing transactions as operations with attempts, the durable nonce allocator, and refund offers (PLAN.md
sections 5, 6 and 11)."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base, BaseUnits, pg_enum
from app.payments.settlements.enums import OperationKind, OperationStatus, OperatorKeyRole, OperatorKeyStatus

ADDRESS_LENGTH = 42
HASH_LENGTH = 66


class OutgoingOperation(Base):
    """A sweep (the factory's ``sweepToDestination`` for one deposit), a refund (a transfer from the refund key's
    float to a recorded sender), or a key operation with no invoice: a top-up from the treasury Safe, a retired key's
    return to it, the treasury Safe's creation. Its nonce is fixed once allocated; it is never retried at another
    one. ``operator_address`` is the key that signs it."""

    __tablename__ = "outgoing_operations"
    __table_args__ = (
        Index(
            "ix_outgoing_operations_active",
            "chain_key",
            "status",
            postgresql_where=text("status NOT IN ('final', 'failed')"),
        ),
        # At most one active top-up per key it fills and asset (2026-10-13; by recipient since 2026-10-14, migration
        # 0022: another key may send it, operator_key_maintenance_service.py).
        Index(
            "ux_outgoing_operations_active_top_up",
            "chain_key",
            "recipient_address",
            "asset_symbol",
            unique=True,
            postgresql_where=text(
                "kind = 'top_up' AND status IN ('pending', 'waiting_for_float', 'broadcast', 'mined')"
            ),
        ),
        Index(
            "ix_outgoing_operations_static_deposit_address_id",
            "static_deposit_address_id",
            postgresql_where=text("static_deposit_address_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[OperationKind] = mapped_column(pg_enum(OperationKind, name="operation_kind"))
    status: Mapped[OperationStatus] = mapped_column(pg_enum(OperationStatus, name="operation_status"))
    # None for the key operations and static deposit sweeps (OperationKind.has_invoice).
    invoice_id: Mapped[str | None] = mapped_column(String(26), index=True)
    # A static deposit sweep's address (PLAN.md section 17; migration 0033); None for every other operation. No ORM
    # foreign key: settlements sits below static_deposit_addresses.
    static_deposit_address_id: Mapped[str | None] = mapped_column(String(26))
    chain_key: Mapped[str] = mapped_column(String(32))
    operator_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    nonce: Mapped[int | None] = mapped_column(BigInteger)
    # Sweep and static deposit sweep: the deposit, its salt and destination. Refund, top-up and treasury return:
    # recipient (a top-up's is its own key, a return's the treasury Safe), asset and amount. Safe creation: recipient
    # is the predicted Safe.
    deposit_address: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    invoice_salt: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    destination_address: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    # Sweep only: tokens the owner left out by hand (``sweep-invoice --skip-token``), never listed even when held, so
    # a token that reverts its transfer (a frozen or paused one) does not hold up the rest of the deposit.
    skipped_token_addresses: Mapped[list[str]] = mapped_column(
        ARRAY(String(ADDRESS_LENGTH)), default=list, server_default=text("'{}'")
    )
    recipient_address: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    asset_symbol: Mapped[str | None] = mapped_column(String(16))
    # None for the native coin.
    token_address: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    amount_base_units: Mapped[int | None] = mapped_column(BaseUnits)
    # The attempt that mined, and what moved as read from its receipt.
    transaction_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH))
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    block_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH))
    moved_base_units: Mapped[int | None] = mapped_column(BaseUnits)
    failure_reason: Mapped[str | None] = mapped_column(String(200))


class OutgoingAttempt(Base):
    """One signed transaction at the operation's nonce, stored before it is broadcast."""

    __tablename__ = "outgoing_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("outgoing_operations.id"), index=True)
    raw_transaction: Mapped[bytes] = mapped_column(LargeBinary)
    transaction_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), unique=True)
    gas_limit: Mapped[int] = mapped_column(BigInteger)
    max_fee_per_gas: Mapped[int] = mapped_column(BaseUnits)
    max_priority_fee_per_gas: Mapped[int] = mapped_column(BaseUnits)
    last_broadcast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt_succeeded: Mapped[bool | None] = mapped_column(Boolean)
    receipt_block_number: Mapped[int | None] = mapped_column(BigInteger)
    receipt_block_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH))
    receipt_gas_used: Mapped[int | None] = mapped_column(BigInteger)
    receipt_effective_gas_price: Mapped[int | None] = mapped_column(BaseUnits)


class OperatorNonce(Base):
    """Hands out each nonce of one operator on one chain exactly once."""

    __tablename__ = "operator_nonces"

    chain_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    operator_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH), primary_key=True)
    next_nonce: Mapped[int] = mapped_column(BigInteger)


class OperatorKeyState(Base):
    """One operator key's role and state on one chain (2026-10-13). A configured key gets its row (active) when the
    watcher first sees it; its ``id`` names it in operator alerts, which never carry an address."""

    __tablename__ = "operator_key_states"
    __table_args__ = (Index("ux_operator_key_states_chain_address", "chain_key", "operator_address", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chain_key: Mapped[str] = mapped_column(String(32))
    operator_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    role: Mapped[OperatorKeyRole] = mapped_column(pg_enum(OperatorKeyRole, name="operator_key_role"))
    status: Mapped[OperatorKeyStatus] = mapped_column(pg_enum(OperatorKeyStatus, name="operator_key_status"))
    reason: Mapped[str] = mapped_column(String(200))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RefundOffer(Base):
    """The text a sender signs to claim their refund; consumed once (PLAN.md section 6)."""

    __tablename__ = "refund_offers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(26), index=True)
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    sender_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    asset_symbol: Mapped[str] = mapped_column(String(16))
    amount_base_units: Mapped[int] = mapped_column(BaseUnits)
    message: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
