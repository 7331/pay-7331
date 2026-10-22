"""What the account service takes and returns."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

# `__Host-`: the browser takes it only with Secure, Path=/ and no Domain, so no subdomain can set or shadow it
# (https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie#cookie_prefixes).
ACCOUNT_SESSION_COOKIE_NAME = "__Host-pay_account_session"


# (session, checksummed address): True when it is one of this gateway's deposit addresses, an invoice's or a static
# one; never a payout address or an invoice destination, since money sent there would only be swept back to it
# (2026-10-21). Built in the static deposit addresses domain and handed down by app/lifespan, as the domains below it
# cannot import it. A plain Callable, as ``InvoiceCallbackUrlCheck``: InvoiceWiring (a pydantic model) holds it.
GatewayDepositAddressCheck = Callable[[AsyncSession, str], Awaitable[bool]]


class AccountAccess(BaseModel):
    """The signed-in account a request came with."""

    model_config = ConfigDict(frozen=True)

    account_id: int
    username: str
    payout_address: str
    account_session_id: int
    account_passkey_id: int
    # Every enabled chain's starting coins: the account's saved ones, else the chain's defaults.
    accepted_asset_symbols_by_chain: dict[str, list[str]]
    # The confirmations an XMR invoice counts final at, per enabled Monero chain key; a chain left out stands at the
    # chain's default.
    confirmations_final_by_chain: dict[str, int]
    # As saved: an email address, a Telegram handle or an https link; None when unset.
    support_contact: str | None

    # Keyword constructors, not model_copy(update=...), which neither ty nor Pydantic checks (semgrep
    # no-model-copy-update).
    def with_payout_address(self, payout_address: str) -> "AccountAccess":
        """The same access after a payout address change."""
        return AccountAccess(
            account_id=self.account_id,
            username=self.username,
            payout_address=payout_address,
            account_session_id=self.account_session_id,
            account_passkey_id=self.account_passkey_id,
            accepted_asset_symbols_by_chain=self.accepted_asset_symbols_by_chain,
            confirmations_final_by_chain=self.confirmations_final_by_chain,
            support_contact=self.support_contact,
        )

    def with_accepted_asset_symbols_by_chain(
        self, accepted_asset_symbols_by_chain: dict[str, list[str]]
    ) -> "AccountAccess":
        """The same access after the accepted coins were saved."""
        return AccountAccess(
            account_id=self.account_id,
            username=self.username,
            payout_address=self.payout_address,
            account_session_id=self.account_session_id,
            account_passkey_id=self.account_passkey_id,
            accepted_asset_symbols_by_chain=accepted_asset_symbols_by_chain,
            confirmations_final_by_chain=self.confirmations_final_by_chain,
            support_contact=self.support_contact,
        )

    def with_confirmations_final_by_chain(self, confirmations_final_by_chain: dict[str, int]) -> "AccountAccess":
        """The same access after the XMR confirmations were saved."""
        return AccountAccess(
            account_id=self.account_id,
            username=self.username,
            payout_address=self.payout_address,
            account_session_id=self.account_session_id,
            account_passkey_id=self.account_passkey_id,
            accepted_asset_symbols_by_chain=self.accepted_asset_symbols_by_chain,
            confirmations_final_by_chain=confirmations_final_by_chain,
            support_contact=self.support_contact,
        )

    def with_support_contact(self, support_contact: str | None) -> "AccountAccess":
        """The same access after the support contact was saved."""
        return AccountAccess(
            account_id=self.account_id,
            username=self.username,
            payout_address=self.payout_address,
            account_session_id=self.account_session_id,
            account_passkey_id=self.account_passkey_id,
            accepted_asset_symbols_by_chain=self.accepted_asset_symbols_by_chain,
            confirmations_final_by_chain=self.confirmations_final_by_chain,
            support_contact=support_contact,
        )


class AccountApiTokenAccess(BaseModel):
    """The account a bearer API token stands for. Only the routes an account's server calls take
    it: its invoices, its quantity links, its invoice and static deposit address events (2026-10-21) and its static
    deposit addresses (PLAN.md section 17); never a route that changes where money
    or notifications go."""

    model_config = ConfigDict(frozen=True)

    account_id: int
    payout_address: str
    account_api_token_id: int
    # As on ``AccountAccess``: what an invoice made with no network or coin field offers.
    accepted_asset_symbols_by_chain: dict[str, list[str]]


class AccountApiTokenSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    # `tok_...`: the id the API sends; the integer one stays internal.
    account_api_token_public_id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None


class CreatedAccountApiToken(BaseModel):
    """A token just made: the plaintext goes out once in this answer and is never stored."""

    model_config = ConfigDict(frozen=True)

    summary: AccountApiTokenSummary
    token: SecretStr


class AccountPasskeySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    # `pk_...`: the id the API sends; the integer one stays internal.
    account_passkey_public_id: str
    label: str
    created_at: datetime
    last_used_at: datetime | None
    # The passkey the asking session signed in with (`account_sessions.account_passkey_id`): the one in hand.
    used_by_current_session: bool


class StartedAccountSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_session_id: int
    # Set as the cookie once; only its SHA-256 is stored.
    session_token: SecretStr
    username: str
    payout_address: str
    accepted_asset_symbols_by_chain: dict[str, list[str]]
    confirmations_final_by_chain: dict[str, int]
    support_contact: str | None


class PayoutAddressChange(BaseModel):
    """A payout address change a fresh passkey check approved: both addresses checksummed."""

    model_config = ConfigDict(frozen=True)

    previous_payout_address: str
    payout_address: str


class SellerSupportContact(BaseModel):
    """An account's support contact as the payer page links it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["email", "telegram", "url"]
    # What the contact reads as: the email address, `@name`, or the link.
    display_text: str
    # `mailto:...`, `https://t.me/name` or the https link itself; never another scheme.
    href: str


class MintedPasskeyEnrollmentLink(BaseModel):
    """A new enrollment link: the whole link carries its token, so it is shown once and never logged."""

    model_config = ConfigDict(frozen=True)

    passkey_enrollment_link_id: int
    link: SecretStr
    expires_at: datetime


class PasskeyEnrollment(BaseModel):
    """What an enrollment through a link made: the passkey and the signed-in session it opened."""

    model_config = ConfigDict(frozen=True)

    account_id: int
    started_session: StartedAccountSession
