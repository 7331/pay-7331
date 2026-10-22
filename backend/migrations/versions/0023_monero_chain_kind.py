"""The Monero chain kind's schema (2026-10-14; PLAN.md section 14): expand only.

- `account_monero_wallets`: an account's primary address and sealed private view key, one active row per account and
  chain (a change inserts a new row, or reactivates the account's own old row of that address, and sets `replaced_at`
  on the old one), one row per address and chain across accounts and replaced rows (so a minor is never derived
  twice), and `confirmed_lookahead_minor_count`, the receiver's confirmed wallet lookahead (200 by default).
- `invoices`: `subaddress_minor` and `monero_wallet_id`, both nullable and both set or both null, and a unique partial
  index so a subaddress is never reused; `deposit_address` and `destination_address` widen from 42 to 106 characters (a varchar widening is a
  catalog change, not a rewrite, and the unique constraint on `deposit_address` stays).
- `payment_links.destination_address` widens the same way (a quantity link's invoices copy it).
- `monero_payments`: the outputs the scanner sidecar finds, unique by chain and output public key.

Nothing the running release reads changes, so the step is additive.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONERO_ADDRESS_COLUMN_LENGTH = 106


def upgrade() -> None:
    monero_network = postgresql.ENUM("mainnet", "stagenet", name="monero_network", create_type=False)
    monero_cross_check_state = postgresql.ENUM(
        "pending", "confirmed", "mismatch", name="monero_cross_check_state", create_type=False
    )
    monero_network.create(op.get_bind(), checkfirst=True)
    monero_cross_check_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "account_monero_wallets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column("primary_address", sa.String(length=95), nullable=False),
        sa.Column("sealed_private_view_key", sa.LargeBinary(), nullable=False),
        sa.Column("network", monero_network, nullable=False),
        sa.Column("major_index", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("next_minor_index", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("confirmed_lookahead_minor_count", sa.Integer(), server_default=sa.text("200"), nullable=False),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_account_monero_wallets_active",
        "account_monero_wallets",
        ["account_id", "chain_key"],
        unique=True,
        postgresql_where=sa.text("replaced_at IS NULL"),
    )
    op.create_index(
        "ux_account_monero_wallets_chain_key_primary_address",
        "account_monero_wallets",
        ["chain_key", "primary_address"],
        unique=True,
    )

    op.alter_column(
        "invoices",
        "deposit_address",
        existing_type=sa.String(length=42),
        type_=sa.String(length=MONERO_ADDRESS_COLUMN_LENGTH),
        existing_nullable=False,
    )
    op.alter_column(
        "invoices",
        "destination_address",
        existing_type=sa.String(length=42),
        type_=sa.String(length=MONERO_ADDRESS_COLUMN_LENGTH),
        existing_nullable=False,
    )
    op.alter_column(
        "payment_links",
        "destination_address",
        existing_type=sa.String(length=42),
        type_=sa.String(length=MONERO_ADDRESS_COLUMN_LENGTH),
        existing_nullable=False,
    )
    op.add_column("invoices", sa.Column("subaddress_minor", sa.Integer(), nullable=True))
    op.add_column(
        "invoices",
        sa.Column("monero_wallet_id", sa.BigInteger(), sa.ForeignKey("account_monero_wallets.id"), nullable=True),
    )
    op.create_index(
        "ux_invoices_monero_wallet_id_subaddress_minor",
        "invoices",
        ["monero_wallet_id", "subaddress_minor"],
        unique=True,
        postgresql_where=sa.text("monero_wallet_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_invoices_monero_wallet_id_subaddress_minor_together",
        "invoices",
        "(monero_wallet_id IS NULL) = (subaddress_minor IS NULL)",
    )

    op.create_table(
        "monero_payments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("txid", sa.String(length=64), nullable=False),
        sa.Column("output_public_key", sa.String(length=64), nullable=False),
        sa.Column("output_index", sa.Integer(), nullable=False),
        sa.Column("amount_piconero", sa.Numeric(78, 0), nullable=False),
        sa.Column("block_height", sa.BigInteger(), nullable=True),
        sa.Column("block_hash", sa.String(length=64), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("in_pool", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("double_spend_seen", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cross_check_state", monero_cross_check_state, server_default="pending", nullable=False),
        sa.Column("cross_check_operator", sa.String(length=64), nullable=True),
        sa.Column("final_cross_check_state", monero_cross_check_state, server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chain_key", "output_public_key", name="uq_monero_payments_chain_key_output_public_key"),
    )
    op.create_index("ix_monero_payments_invoice_id", "monero_payments", ["invoice_id"])
    op.create_index(
        "ix_monero_payments_chain_key_uncredited",
        "monero_payments",
        ["chain_key"],
        postgresql_where=sa.text("credited_at IS NULL"),
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
