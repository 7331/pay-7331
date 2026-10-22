"""The confirmation count an XMR invoice counts final at is the receiver's choice: expand only.

- `accounts.confirmations_final_by_chain`: the confirmations-before-final the account sets per Monero chain key,
  `{"monero": 3}`; `{}` when unset, so every chain stands at its `chains.toml` default.
- `invoices.confirmations_final`: the count the invoice counts its outputs final at, set when a Monero target is
  allocated (its chain's default or the account's saved one) and read by the watcher and the scanner sidecar after
  that. Null on EVM invoices, on Monero invoices written by the previous release (which stand at the chain default),
  and while an invoice waits for the payer's pick: the column fills in with the deposit at the pick.

Nothing the running release reads changes, so the step is additive.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "confirmations_final_by_chain",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("invoices", sa.Column("confirmations_final", sa.Integer(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
