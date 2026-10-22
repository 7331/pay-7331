"""AccountApiTokenService: bearer tokens an account's own server uses on the invoice routes, e.g. a
shop's backend (PLAN.md section 7).

- Made in ``/settings`` behind a fresh passkey check (``AccountService.verify_fresh_passkey_check``, purpose
  ``create_api_token``), so a stolen session cannot mint one. At most ``MAXIMUM_API_TOKENS_PER_ACCOUNT`` per account.
- A token is ``pay_tok_`` plus 32 random bytes in unpadded base64url (``secrets.token_urlsafe(32)``, 43 characters;
  https://docs.python.org/3/library/secrets.html#secrets.token_urlsafe). It is shown once; only its SHA-256 is
  stored, which is enough for a 256-bit random secret (no salt or slow hash needed: nothing to guess).
- ``Authorization: Bearer pay_tok_...`` (RFC 6750 section 2.1, https://www.rfc-editor.org/rfc/rfc6750#section-2.1)
  authenticates as its account on ``POST /invoices``, ``GET /invoices`` and ``GET /invoices/{id}`` only. Bearer
  requests carry no cookie, so the cookie's Origin check does not apply. A malformed, unknown or revoked token is 401
  ``api_token_invalid``. Revoking deletes the row.
- ``last_used_at`` is written at most once a minute per token.
"""

from datetime import UTC, datetime, timedelta
import re
import secrets
from typing import Self

from pydantic import JsonValue, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.accounts import log_events
from app.payments.accounts.enums import ChallengePurpose
from app.payments.accounts.errors import (
    AccountApiTokenNotFoundError,
    ApiTokenInvalidError,
    TooManyApiTokensError,
)
from app.payments.accounts.models import AccountApiToken
from app.payments.accounts.preferences_service import AccountPreferencesService
from app.payments.accounts.repository import AccountRepository
from app.payments.accounts.service import AccountService, hash_secret_token
from app.payments.accounts.types import (
    AccountAccess,
    AccountApiTokenAccess,
    AccountApiTokenSummary,
    CreatedAccountApiToken,
)
from app.shared import logging as log
from app.shared.errors import AppError

logger = log.get_logger(__name__)

API_TOKEN_PREFIX = "pay_tok_"  # noqa: S105  # the format prefix, not a secret
API_TOKEN_RANDOM_BYTES = 32
# The prefix and at least 32 bytes of unpadded base64url; longer is accepted, so a future longer token still parses.
API_TOKEN_PATTERN = re.compile(r"pay_tok_[A-Za-z0-9_-]{43,128}")
MAXIMUM_API_TOKENS_PER_ACCOUNT = 20
LAST_USED_WRITE_INTERVAL = timedelta(minutes=1)


def present_api_token(account_api_token: AccountApiToken) -> AccountApiTokenSummary:
    return AccountApiTokenSummary(
        account_api_token_public_id=account_api_token.public_id,
        name=account_api_token.name,
        created_at=account_api_token.created_at,
        last_used_at=account_api_token.last_used_at,
    )


def read_bearer_api_token(authorization_header: str | None) -> SecretStr | None:
    """The token of an ``Authorization: Bearer ...`` header (the scheme matches case-insensitively, RFC 6750 section
    2.1 and RFC 9110 section 11.1); None for no header or another scheme."""
    if authorization_header is None:
        return None
    scheme, _, credentials = authorization_header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return SecretStr(credentials.strip())


class AccountApiTokenService:
    def __init__(
        self,
        *,
        repository: AccountRepository,
        account_service: AccountService,
        account_preferences_service: AccountPreferencesService,
    ) -> None:
        self.repository = repository
        self.account_service = account_service
        self.account_preferences_service = account_preferences_service

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        account_service = AccountService.on_session(session)
        return cls(
            repository=AccountRepository(session=session),
            account_service=account_service,
            account_preferences_service=account_service.account_preferences_service,
        )

    async def create_api_token_options(self, account_access: AccountAccess) -> dict[str, JsonValue]:
        """Fresh passkey check options for a new token (refused up front when the account has the most it may)."""
        await self.refuse_past_token_limit(account_access.account_id)
        return await self.account_service.create_fresh_passkey_check_options(
            account_access, purpose=ChallengePurpose.CREATE_API_TOKEN
        )

    async def refuse_past_token_limit(self, account_id: int) -> None:
        if await self.repository.count_account_api_tokens(account_id) >= MAXIMUM_API_TOKENS_PER_ACCOUNT:
            raise TooManyApiTokensError(
                f"an account has at most {MAXIMUM_API_TOKENS_PER_ACCOUNT} API tokens; revoke one first"
            )

    async def create_api_token(
        self, account_access: AccountAccess, *, name: str, credential_json: dict[str, JsonValue]
    ) -> CreatedAccountApiToken:
        """A new token once the ``create_api_token`` passkey check verifies; its plaintext is in the answer only."""
        try:
            # Before the passkey check too, so the one refusal left after it is a creation that landed meanwhile.
            await self.refuse_past_token_limit(account_access.account_id)
            account, account_passkey = await self.account_service.verify_fresh_passkey_check(
                account_access, purpose=ChallengePurpose.CREATE_API_TOKEN, credential_json=credential_json
            )
            # Under the account row lock the check took, so two creations cannot both pass the limit.
            await self.refuse_past_token_limit(account.id)
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="create_api_token", reason=refusal.code))
            raise
        token = API_TOKEN_PREFIX + secrets.token_urlsafe(API_TOKEN_RANDOM_BYTES)
        account_api_token = await self.repository.insert_api_token(
            AccountApiToken(account_id=account.id, name=name.strip(), token_hash=hash_secret_token(token))
        )
        log.emit(
            logger,
            event=log_events.AccountApiTokenCreated(
                account_id=account.id,
                account_api_token_id=account_api_token.id,
                account_passkey_id=account_passkey.id,
            ),
        )
        return CreatedAccountApiToken(summary=present_api_token(account_api_token), token=SecretStr(token))

    async def list_api_tokens(self, account_access: AccountAccess) -> list[AccountApiTokenSummary]:
        return [
            present_api_token(account_api_token)
            for account_api_token in await self.repository.list_account_api_tokens(account_access.account_id)
        ]

    async def revoke_api_token(self, account_access: AccountAccess, *, account_api_token_public_id: str) -> None:
        account_api_token_id = await self.repository.delete_account_api_token(
            account_api_token_public_id, account_id=account_access.account_id
        )
        if account_api_token_id is None:
            raise AccountApiTokenNotFoundError(f"no API token {account_api_token_public_id} on this account")
        log.emit(
            logger,
            event=log_events.AccountApiTokenRevoked(
                account_id=account_access.account_id, account_api_token_id=account_api_token_id
            ),
        )

    async def authenticate_api_token(self, api_token: SecretStr) -> AccountApiTokenAccess:
        """The account a bearer token stands for; 401 ``api_token_invalid`` when malformed, unknown or revoked."""
        token_text = api_token.get_secret_value()
        if not API_TOKEN_PATTERN.fullmatch(token_text):
            log.emit(logger, event=log_events.AccountApiTokenRefused(reason="malformed"))
            raise ApiTokenInvalidError("the API token is malformed; it starts with pay_tok_")
        found = await self.repository.find_api_token_with_account(hash_secret_token(token_text))
        if found is None:
            log.emit(logger, event=log_events.AccountApiTokenRefused(reason="unknown"))
            raise ApiTokenInvalidError("the API token is unknown or was revoked")
        account_api_token, account = found
        account_preferences_service = self.account_preferences_service
        now = datetime.now(UTC)
        await self.repository.record_api_token_use(
            account_api_token.id, used_at=now, used_before=now - LAST_USED_WRITE_INTERVAL
        )
        return AccountApiTokenAccess(
            account_id=account.id,
            payout_address=account.payout_address,
            account_api_token_id=account_api_token.id,
            accepted_asset_symbols_by_chain=await account_preferences_service.resolve_accepted_asset_symbols_by_chain(
                account.id, saved_by_chain=account.accepted_asset_symbols_by_chain
            ),
        )

    async def find_api_token_account_id(self, api_token: SecretStr) -> int | None:
        """The rate limiter's read: the token's account, or None for a malformed, unknown or revoked one. No write."""
        token_text = api_token.get_secret_value()
        if not API_TOKEN_PATTERN.fullmatch(token_text):
            return None
        return await self.repository.find_api_token_account_id(hash_secret_token(token_text))
