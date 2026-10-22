"""Accounts, their passkeys, the sessions those open, the single-use WebAuthn challenges and the API tokens an
account's own server uses (PLAN.md section 7)."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base
from app.shared.identifiers import generate_public_id

ADDRESS_LENGTH = 42
USERNAME_MAXIMUM_LENGTH = 20
SUPPORT_CONTACT_MAXIMUM_LENGTH = 200
ACCOUNT_PASSKEY_PUBLIC_ID_PREFIX = "pk_"
ACCOUNT_API_TOKEN_PUBLIC_ID_PREFIX = "tok_"  # noqa: S105  # an id prefix, not a secret


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (CheckConstraint("username ~ '^[a-z0-9_]{3,20}$'", name="ck_accounts_username_format"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Stored lowercased, so unique is unique case-insensitively. The payout address's alias; never on payer pages.
    username: Mapped[str] = mapped_column(String(USERNAME_MAXIMUM_LENGTH), unique=True)
    # Checksummed; `/` prefills it as the receiver. Not verified.
    payout_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    # The WebAuthn user.id of this account's passkeys: random, so one authenticator can hold passkeys of two accounts.
    passkey_user_handle: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    # The coins this account's invoices start with ticked, per chain key: {"polygon": ["POL", "USDC"]}. A chain with no
    # entry starts from the chain's defaults (migration 0009).
    accepted_asset_symbols_by_chain: Mapped[dict[str, list[str]]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    # The confirmations an XMR invoice of this account counts final at, per Monero chain key: {"monero": 3}. A chain
    # with no entry stands at the chain's chains.toml default; the receiver alone carries the double-spend risk, so it
    # is the receiver's choice (migration 0028).
    confirmations_final_by_chain: Mapped[dict[str, int]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # The payment window (seconds) and the accepted shortfall (percent, a decimal string) this account's invoices take
    # per chain key when their maker sets none: {"polygon": 3600} and {"polygon": "0.5"}. A chain with no entry takes
    # the server's default (migration 0037).
    invoice_lifetime_seconds_by_chain: Mapped[dict[str, int]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    accuracy_percent_by_chain: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Shown to the payers of this account's invoices: an email address, a Telegram handle (`@name`) or an https link,
    # as typed (migration 0011). Null: none.
    support_contact: Mapped[str | None] = mapped_column(String(SUPPORT_CONTACT_MAXIMUM_LENGTH))


class AccountPasskey(Base):
    __tablename__ = "account_passkeys"
    __table_args__ = (Index("ux_account_passkeys_public_id", "public_id", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The passkey's id on the wire (`pk_...`, migration 0036); the integer id stays internal.
    public_id: Mapped[str] = mapped_column(
        String(26),
        default=lambda: generate_public_id(ACCOUNT_PASSKEY_PUBLIC_ID_PREFIX),
        server_default=text(f"generate_public_id('{ACCOUNT_PASSKEY_PUBLIC_ID_PREFIX}')"),
    )
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    # The authenticator's credential id (raw bytes); sign-in finds the passkey, and so the account, by it.
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    # COSE-encoded public key, as py_webauthn returns and takes it.
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger)
    # WebAuthn transport hints ("internal", "hybrid", "usb", ...), handed back in registration's excludeCredentials.
    transports: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    label: Mapped[str] = mapped_column(String(100))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountSession(Base):
    __tablename__ = "account_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # SHA-256 hex of the cookie's token; the token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Deleting a passkey (or its account) ends the sessions it opened.
    account_passkey_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account_passkeys.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PasskeyChallenge(Base):
    __tablename__ = "passkey_challenges"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # SHA-256 hex of the challenge bytes; the verify step finds the row from the challenge in clientDataJSON.
    challenge_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # ChallengePurpose: a challenge answers only the ceremony it was made for.
    purpose: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # The signed-in account the challenge was made for (every purpose but `sign_up` and `sign_in`).
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"))
    # `sign_up`: the account asked for, made only when the passkey verifies.
    sign_up_username: Mapped[str | None] = mapped_column(String(USERNAME_MAXIMUM_LENGTH))
    sign_up_payout_address: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    sign_up_user_handle: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    # `change_payout`: the checksummed address the passkey check approves (migration 0011).
    requested_payout_address: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))
    # `change_monero`: SHA-256 hex of `chain_key|primary_address`, the wallet the passkey check approves (migration
    # 0025). A digest, not the address: the row needs to match a request, never to show the wallet. `delete_passkey`:
    # SHA-256 hex of `account_passkey|<id>`, the passkey the check approves deleting.
    requested_change_digest: Mapped[str | None] = mapped_column(String(64))


class AccountApiToken(Base):
    """A bearer token for the account's own server (migration 0015). Revoking deletes the row."""

    __tablename__ = "account_api_tokens"
    __table_args__ = (Index("ux_account_api_tokens_public_id", "public_id", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The token's id on the wire (`tok_...`, migration 0036); the integer id stays internal.
    public_id: Mapped[str] = mapped_column(
        String(26),
        default=lambda: generate_public_id(ACCOUNT_API_TOKEN_PUBLIC_ID_PREFIX),
        server_default=text(f"generate_public_id('{ACCOUNT_API_TOKEN_PUBLIC_ID_PREFIX}')"),
    )
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    # What the account calls it ("My shop"); shown in /settings.
    name: Mapped[str] = mapped_column(String(100))
    # SHA-256 hex of the whole `pay_tok_...` token; the token itself is shown once and never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Updated at most once a minute (AccountApiTokenService), so a busy server does not write on every request.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountPasskeyEnrollmentLink(Base):
    """A one-time link the operator mints (``python -m app.cli mint-passkey-enrollment-link``) so a person can add a
    passkey to an account without signing in: the account recovery path (2026-10-15; migration 0029)."""

    __tablename__ = "account_passkey_enrollment_links"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    # SHA-256 hex of the link's token; the token itself is printed once and never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Set by the enrollment that used it; a used link stays, so opening it again says it was used.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
