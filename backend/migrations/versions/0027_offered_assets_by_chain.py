"""The payer picks the network (2026-10-14; PLAN.md section 15): expand only.

- `invoices.offered_assets_by_chain` and `payment_links.offered_assets_by_chain`: the coins each offered chain accepts,
  `{"polygon": ["USDC", "POL"], "arbitrum": ["USDC"]}`, in the order the maker offered them. Every existing row is
  backfilled to its one chain and its `assets`; the column default `{}` is what a row written by the previous release
  gets, and the code reads an empty map as "its `chain_key` with its `assets`", so the two releases overlap safely.
  `chain_key` and `assets` stay as they were: the first offered chain until the payer picks, then the chosen one.
- `invoices.deposit_address` becomes nullable: an invoice offering several chains derives its deposit when the payer
  picks a (chain, asset) pair. The new check keeps the one rule the watcher relies on, that an invoice with a coin
  picked has its deposit (existing rows all do).

Nothing the running release reads changes, so the step is additive.

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name in ("invoices", "payment_links"):
        op.add_column(
            table_name,
            sa.Column(
                "offered_assets_by_chain",
                postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
        )
        op.execute(
            sa.text(
                f"UPDATE {table_name} SET offered_assets_by_chain = jsonb_build_object(chain_key, to_jsonb(assets))"  # noqa: S608
            )
        )
    op.alter_column("invoices", "deposit_address", existing_type=sa.String(length=106), nullable=True)
    op.create_check_constraint(
        "ck_invoices_asset_symbol_requires_deposit_address",
        "invoices",
        "asset_symbol IS NULL OR deposit_address IS NOT NULL",
    )


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
