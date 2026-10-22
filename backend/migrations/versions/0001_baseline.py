"""Baseline: every table of PLAN.md section 11. Asset amounts are numeric(78,0) base units, fiat numeric(20,2).

Revision ID: 0001
Revises: none
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_table(
        "chain_cursors",
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column(
            "stream_name",
            sa.Enum("finalized_head", "deposit_swept_logs", "token_transfer_logs", name="cursor_stream"),
            nullable=False,
        ),
        sa.Column("last_block_number", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chain_key", "stream_name"),
    )
    op.create_table(
        "deposit_sweep_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column("transaction_hash", sa.String(length=66), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("deposit_address", sa.String(length=42), nullable=False),
        sa.Column("destination_address", sa.String(length=42), nullable=False),
        sa.Column("token_address", sa.String(length=42), nullable=True),
        sa.Column("amount_base_units", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("block_hash", sa.String(length=66), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chain_key", "transaction_hash", "log_index"),
    )
    op.create_index(
        "ix_deposit_sweep_events_deposit", "deposit_sweep_events", ["chain_key", "deposit_address"], unique=False
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "awaiting_asset",
                "awaiting_payment",
                "confirming",
                "underpaid",
                "paid",
                "overpaid",
                "expired",
                "cancelled",
                name="invoice_status",
            ),
            nullable=False,
        ),
        sa.Column("partial", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("late_payment", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sweep_blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("assets", postgresql.ARRAY(sa.String(length=16)), nullable=False),
        sa.Column("asset_symbol", sa.String(length=16), nullable=True),
        sa.Column("invoice_salt", sa.LargeBinary(length=32), nullable=False),
        sa.Column("deposit_address", sa.String(length=42), nullable=False),
        sa.Column("destination_address", sa.String(length=42), nullable=False),
        sa.Column("fiat_currency", sa.String(length=3), nullable=True),
        sa.Column("fiat_amount", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("crypto_amount_base_units", sa.Numeric(precision=78, scale=0), nullable=True),
        sa.Column("amount_due_base_units", sa.Numeric(precision=78, scale=0), nullable=True),
        sa.Column(
            "received_base_units", sa.Numeric(precision=78, scale=0), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "credited_fiat_amount", sa.Numeric(precision=38, scale=18), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("excess_base_units", sa.Numeric(precision=78, scale=0), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "latest_gross_base_units", sa.Numeric(precision=78, scale=0), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "latest_balance_base_units", sa.Numeric(precision=78, scale=0), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("latest_reading_block_number", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(length=500), server_default=sa.text("''"), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("webhook_url", sa.String(length=2048), nullable=True),
        sa.Column("return_url", sa.String(length=2048), nullable=True),
        sa.Column("lifetime_seconds", sa.Integer(), nullable=False),
        sa.Column("accuracy_percent", sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("underpaid_open_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_link_id", sa.String(length=25), nullable=True),
        sa.Column("client_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deposit_address"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index(
        "ix_invoices_chain_key_open",
        "invoices",
        ["chain_key"],
        unique=False,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "ix_invoices_client_ip_hash_open",
        "invoices",
        ["client_ip_hash"],
        unique=False,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index("ix_invoices_created_at_id", "invoices", ["created_at", "id"], unique=False)
    op.create_index(op.f("ix_invoices_payment_link_id"), "invoices", ["payment_link_id"], unique=False)
    op.create_table(
        "operator_nonces",
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column("operator_address", sa.String(length=42), nullable=False),
        sa.Column("next_nonce", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chain_key", "operator_address"),
    )
    op.create_table(
        "outgoing_operations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Enum("sweep", "refund", name="operation_kind"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "waiting_for_float", "broadcast", "mined", "final", "failed", name="operation_status"),
            nullable=False,
        ),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column("operator_address", sa.String(length=42), nullable=False),
        sa.Column("nonce", sa.BigInteger(), nullable=True),
        sa.Column("deposit_address", sa.String(length=42), nullable=True),
        sa.Column("invoice_salt", sa.LargeBinary(length=32), nullable=True),
        sa.Column("destination_address", sa.String(length=42), nullable=True),
        sa.Column("recipient_address", sa.String(length=42), nullable=True),
        sa.Column("asset_symbol", sa.String(length=16), nullable=True),
        sa.Column("token_address", sa.String(length=42), nullable=True),
        sa.Column("amount_base_units", sa.Numeric(precision=78, scale=0), nullable=True),
        sa.Column("transaction_hash", sa.String(length=66), nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("block_hash", sa.String(length=66), nullable=True),
        sa.Column("moved_base_units", sa.Numeric(precision=78, scale=0), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outgoing_operations_active",
        "outgoing_operations",
        ["chain_key", "status"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('final', 'failed')"),
    )
    op.create_index(op.f("ix_outgoing_operations_invoice_id"), "outgoing_operations", ["invoice_id"], unique=False)
    op.create_table(
        "payment_links",
        sa.Column("id", sa.String(length=25), nullable=False),
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column("assets", postgresql.ARRAY(sa.String(length=16)), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("minimum_amount", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("maximum_amount", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("destination_address", sa.String(length=42), nullable=False),
        sa.Column("allow_contract_destination", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=False),
        sa.Column("webhook_url", sa.String(length=2048), nullable=True),
        sa.Column("return_url", sa.String(length=2048), nullable=True),
        sa.Column("lifetime_seconds", sa.Integer(), nullable=False),
        sa.Column("accuracy_percent", sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column("screening_block_proxy", sa.Boolean(), nullable=True),
        sa.Column("screening_block_hosting", sa.Boolean(), nullable=True),
        sa.Column("screening_blocked_country_codes", postgresql.ARRAY(sa.String(length=2)), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "price_quotes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("chain_key", sa.String(length=32), nullable=False),
        sa.Column("asset_symbol", sa.String(length=16), nullable=False),
        sa.Column("fiat_currency", sa.String(length=3), nullable=False),
        sa.Column("asset_price_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("fiat_price_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("eur_price_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("fiat_amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("amount_base_units", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_price_quotes_invoice_id"), "price_quotes", ["invoice_id"], unique=False)
    op.create_table(
        "refund_offers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("sender_address", sa.String(length=42), nullable=False),
        sa.Column("asset_symbol", sa.String(length=16), nullable=False),
        sa.Column("amount_base_units", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nonce"),
    )
    op.create_index(op.f("ix_refund_offers_invoice_id"), "refund_offers", ["invoice_id"], unique=False)
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.Enum("pending", "delivered", "dead", name="webhook_delivery_status"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_response_status", sa.Integer(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(op.f("ix_webhook_deliveries_invoice_id"), "webhook_deliveries", ["invoice_id"], unique=False)
    op.create_table(
        "invoice_payments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("transaction_hash", sa.String(length=66), nullable=True),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("transaction_index", sa.Integer(), nullable=False),
        sa.Column("sender", sa.String(length=42), nullable=True),
        sa.Column("sender_has_code", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("amount_base_units", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("block_hash", sa.String(length=66), nullable=False),
        sa.Column("final", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id", "transaction_hash", "log_index"),
    )
    op.create_index(op.f("ix_invoice_payments_invoice_id"), "invoice_payments", ["invoice_id"], unique=False)
    op.create_table(
        "outgoing_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("operation_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_transaction", sa.LargeBinary(), nullable=False),
        sa.Column("transaction_hash", sa.String(length=66), nullable=False),
        sa.Column("gas_limit", sa.BigInteger(), nullable=False),
        sa.Column("max_fee_per_gas", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("max_priority_fee_per_gas", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("last_broadcast_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_succeeded", sa.Boolean(), nullable=True),
        sa.Column("receipt_block_number", sa.BigInteger(), nullable=True),
        sa.Column("receipt_block_hash", sa.String(length=66), nullable=True),
        sa.Column("receipt_gas_used", sa.BigInteger(), nullable=True),
        sa.Column("receipt_effective_gas_price", sa.Numeric(precision=78, scale=0), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["outgoing_operations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_hash"),
    )
    op.create_index(op.f("ix_outgoing_attempts_operation_id"), "outgoing_attempts", ["operation_id"], unique=False)


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
