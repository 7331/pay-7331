"""An account's Monero wallet: the primary address and sealed private view key the gateway derives each XMR
invoice's subaddress from (PLAN.md section 14, migration 0023). The server holds a view key only: it can watch, never
spend."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, Integer, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base, pg_enum
from app.payments.monero_wallets.enums import MoneroNetwork

# A standard address is 95 characters (an integrated one 106, a subaddress 95 too;
# https://docs.getmonero.org/public-address/standard-address/).
PRIMARY_ADDRESS_LENGTH = 95


class AccountMoneroWallet(Base):
    """Changing the wallet inserts a new row and sets ``replaced_at`` on the old one: invoices keep theirs. Going back
    to an address the account held before reactivates that row with its counters, so no minor is derived twice."""

    __tablename__ = "account_monero_wallets"
    __table_args__ = (
        # One active wallet per account and chain; replaced rows pile up beside it.
        Index(
            "ux_account_monero_wallets_active",
            "account_id",
            "chain_key",
            unique=True,
            postgresql_where=text("replaced_at IS NULL"),
        ),
        # One row per address and chain, across accounts and replaced rows: a second row would restart the minors at 1
        # and derive subaddresses older invoices already use, so reactivating the row is the only way back.
        Index("ux_account_monero_wallets_chain_key_primary_address", "chain_key", "primary_address", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"))
    chain_key: Mapped[str] = mapped_column(String(32))
    primary_address: Mapped[str] = mapped_column(String(PRIMARY_ADDRESS_LENGTH))
    # AES-GCM sealed under a key derived from PAY_INVOICE_SALT_SEED (secret_sealing.py); never logged.
    sealed_private_view_key: Mapped[bytes] = mapped_column(LargeBinary)
    network: Mapped[MoneroNetwork] = mapped_column(pg_enum(MoneroNetwork, name="monero_network"))
    # The gateway uses account (major) index 1 and minors from 1 upward, one per invoice, never reused.
    major_index: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    next_minor_index: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    # The receiver's wallet lookahead in minors the gap guard trusts (types.py): wallet2's default 200 until the
    # receiver confirms `set subaddress-lookahead 50:1000` in the sheet, then 1000.
    confirmed_lookahead_minor_count: Mapped[int] = mapped_column(Integer, server_default=text("200"))
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
