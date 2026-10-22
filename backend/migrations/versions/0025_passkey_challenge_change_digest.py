"""A Monero wallet change's passkey check names the wallet it approves (2026-10-14; PLAN.md section 14): expand only.

- `passkey_challenges.requested_change_digest`: SHA-256 hex of `chain_key|primary_address` for a `change_monero`
  challenge, so an assertion sets only the wallet it was asked for, as `requested_payout_address` does for the payout
  address. Null on every other purpose and on every row made before this revision (those expire within minutes).

A nullable column the running release neither reads nor writes, so the step is additive.

Revision ID: 0025
Revises: 0024
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("passkey_challenges", sa.Column("requested_change_digest", sa.String(64), nullable=True))


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
