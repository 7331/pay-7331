"""Payout address change behind a fresh passkey check, and the seller's support contact (PLAN.md
section 7).

- `passkey_challenges.requested_payout_address`: a `change_payout` challenge carries the address it approves, so a
  passkey assertion changes the payout address only to the address it was asked for.
- `account_events.invoice_id` becomes nullable: an account event that is about the account, not an invoice
  (`account.payout_address_changed`), has none.
- `accounts.support_contact`: an email address, a Telegram handle or an https link (at most 200 characters), shown to
  the payers of the account's invoices as "Need help? Contact the seller". Null for every existing account.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("passkey_challenges", sa.Column("requested_payout_address", sa.String(42), nullable=True))
    op.alter_column("account_events", "invoice_id", existing_type=sa.String(26), nullable=True)
    op.add_column("accounts", sa.Column("support_contact", sa.String(200), nullable=True))


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
