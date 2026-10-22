"""Random public ids for account events (`evt_`), passkeys (`pk_`), API tokens (`tok_`) and webhook deliveries
(`whd_`): a prefix and 22 base58 characters, as invoices have (app/shared/identifiers.py). The API sends only these;
the integer primary keys stay the internal keys for ordering and joins, and are not sent (a delivery queued earlier
keeps the numeric webhook-id it was first sent with).

The SQL function `generate_public_id(prefix)` makes them here: the same alphabet and length as the application's
helper, each character one byte of `gen_random_uuid()` (PostgreSQL's strong random source,
https://www.postgresql.org/docs/current/functions-uuid.html) with the version and variant bytes skipped and bytes of
232 and above rejected, so every character is uniform. It needs no application code, so this revision replays as
written. Per table: `public_id` is added nullable, one UPDATE gives every existing row a fresh id, then the column is
made NOT NULL under `ux_<table>_public_id`. The function stays as the column's default: the release running while this
migrates inserts these rows without a `public_id` until it is replaced. The application always sets the id itself.

All four tables are locked ACCESS EXCLUSIVE up front, in one statement and in the order the application takes them
(passkeys before events, https://www.postgresql.org/docs/current/sql-lock.html), held until this revision's
transaction commits: a reader never sees a row without an id, a passkey added or deleted meanwhile cannot deadlock
with it, and a deploy blocked behind a long transaction fails within the lock timeout instead of queueing every
request. These tables are small, so the UPDATEs are short.

Revision ID: 0036
Revises: 0035
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PUBLIC_ID_PREFIX_BY_TABLE = {
    "account_events": "evt_",
    "account_passkeys": "pk_",
    "account_api_tokens": "tok_",
    "account_webhook_deliveries": "whd_",
}

# Bytes 6 and 8 of a version 4 UUID carry its version and variant bits; the other 14 are random (RFC 9562 section
# 5.4). 232 is the largest multiple of 58 below 256.
CREATE_GENERATE_PUBLIC_ID_FUNCTION = """
CREATE FUNCTION generate_public_id(prefix text) RETURNS text
LANGUAGE plpgsql VOLATILE PARALLEL SAFE AS $$
DECLARE
    base58_alphabet constant text := '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
    random_bytes bytea := uuid_send(gen_random_uuid());
    byte_index integer := 0;
    random_byte integer;
    random_part text := '';
BEGIN
    WHILE length(random_part) < 22 LOOP
        IF byte_index = 16 THEN
            random_bytes := uuid_send(gen_random_uuid());
            byte_index := 0;
        END IF;
        IF byte_index <> 6 AND byte_index <> 8 THEN
            random_byte := get_byte(random_bytes, byte_index);
            IF random_byte < 232 THEN
                random_part := random_part || substr(base58_alphabet, random_byte % 58 + 1, 1);
            END IF;
        END IF;
        byte_index := byte_index + 1;
    END LOOP;
    RETURN prefix || random_part;
END
$$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE account_passkeys, account_api_tokens, account_events, account_webhook_deliveries"
        " IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(CREATE_GENERATE_PUBLIC_ID_FUNCTION)
    for table_name, prefix in PUBLIC_ID_PREFIX_BY_TABLE.items():
        op.add_column(table_name, sa.Column("public_id", sa.String(26), nullable=True))
        op.execute(f"UPDATE {table_name} SET public_id = generate_public_id('{prefix}')")  # noqa: S608  # names and prefixes from the mapping above
        op.alter_column(
            table_name,
            "public_id",
            nullable=False,
            server_default=sa.text(f"generate_public_id('{prefix}')"),
        )
        op.create_index(f"ux_{table_name}_public_id", table_name, ["public_id"], unique=True)


def downgrade() -> None:
    raise RuntimeError("forward only: fix forward with a new revision")
