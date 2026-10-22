"""A random public id for static deposit payments (`sdp_`): a prefix and 22 base58 characters, as migration 0036 gave
events, passkeys, tokens and deliveries. The API and the `static_address.*` webhooks send only it; the integer primary
key stays the internal key for ordering and joins.

`generate_public_id(prefix)`, the SQL function 0036 created, fills every existing row and stays the column's default,
so the release running while this migrates can still insert payments. The table is locked ACCESS EXCLUSIVE up front
and a blocked deploy fails within the lock timeout instead of queueing the watcher behind it.

Revision ID: 0038
Revises: 0037
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATIC_DEPOSIT_PAYMENT_PUBLIC_ID_PREFIX = "sdp_"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE static_deposit_payments IN ACCESS EXCLUSIVE MODE")
    op.add_column("static_deposit_payments", sa.Column("public_id", sa.String(26), nullable=True))
    op.execute(
        f"UPDATE static_deposit_payments SET public_id = generate_public_id('{STATIC_DEPOSIT_PAYMENT_PUBLIC_ID_PREFIX}')"  # noqa: S608  # a constant prefix
    )
    op.alter_column(
        "static_deposit_payments",
        "public_id",
        nullable=False,
        server_default=sa.text(f"generate_public_id('{STATIC_DEPOSIT_PAYMENT_PUBLIC_ID_PREFIX}')"),
    )
    op.create_index("ux_static_deposit_payments_public_id", "static_deposit_payments", ["public_id"], unique=True)


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
