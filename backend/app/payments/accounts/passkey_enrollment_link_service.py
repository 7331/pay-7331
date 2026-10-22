"""PasskeyEnrollmentLinkService: one-time links that add a passkey to an account without signing in, the account
recovery path (2026-10-15). Sign-in is passkey only, so an account whose passkeys are all gone (or an account a script
made with a headless virtual passkey) has no other way back in.

- The operator mints a link from the shell (``python -m app.cli mint-passkey-enrollment-link <username>``); nothing
  on the web mints one. Minting deletes the account's unused links, so only the newest works.
- The link is ``<public origin>/enroll-passkey#<token>``: the token sits in the fragment, which browsers never send,
  so no access log or Referer carries it (RFC 3986 section 3.5, https://www.rfc-editor.org/rfc/rfc3986#section-3.5).
  The token is 32 random bytes in unpadded base64url (``secrets.token_urlsafe(32)``); only its SHA-256 is stored, as
  for sessions and API tokens.
- The page posts the token for registration options (an ``enroll_passkey`` challenge bound to the account and to this
  link's token hash), runs the same registration ceremony as "add a passkey" (``AccountService``'s options, verify and
  insert), and posts the credential with the token. That marks the link used and signs the browser in, in one
  transaction; the link row is locked first, so two enrollments with one link queue and the second is refused.
- A used link and an expired one each say so (410); one never minted, or replaced by a newer one, is 404.
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
    AccountNotFoundError,
    PasskeyChallengeExpiredError,
    PasskeyEnrollmentLinkExpiredError,
    PasskeyEnrollmentLinkUnknownError,
    PasskeyEnrollmentLinkUsedError,
)
from app.payments.accounts.models import AccountPasskeyEnrollmentLink
from app.payments.accounts.repository import AccountRepository
from app.payments.accounts.service import AccountService, hash_secret_token
from app.payments.accounts.types import MintedPasskeyEnrollmentLink, PasskeyEnrollment
from app.shared import logging as log
from app.shared.errors import AppError

logger = log.get_logger(__name__)

PASSKEY_ENROLLMENT_LINK_PATH = "/enroll-passkey"
PASSKEY_ENROLLMENT_TOKEN_RANDOM_BYTES = 32
PASSKEY_ENROLLMENT_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class PasskeyEnrollmentLinkService:
    def __init__(self, *, repository: AccountRepository, account_service: AccountService) -> None:
        self.repository = repository
        self.account_service = account_service

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(repository=AccountRepository(session=session), account_service=AccountService.on_session(session))

    async def mint_passkey_enrollment_link(self, username: str, *, lifetime: timedelta) -> MintedPasskeyEnrollmentLink:
        account = await self.repository.find_account_by_username(username.lower())
        if account is None:
            raise AccountNotFoundError(f"no account is called {username.lower()}")
        token = secrets.token_urlsafe(PASSKEY_ENROLLMENT_TOKEN_RANDOM_BYTES)
        enrollment_link = await self.repository.replace_unused_passkey_enrollment_links(
            AccountPasskeyEnrollmentLink(
                account_id=account.id, token_hash=hash_secret_token(token), expires_at=datetime.now(UTC) + lifetime
            )
        )
        log.emit(
            logger,
            event=log_events.PasskeyEnrollmentLinkMinted(
                account_id=account.id, passkey_enrollment_link_id=enrollment_link.id
            ),
        )
        public_origin = self.account_service.relying_party.expected_origin
        return MintedPasskeyEnrollmentLink(
            passkey_enrollment_link_id=enrollment_link.id,
            link=SecretStr(f"{public_origin}{PASSKEY_ENROLLMENT_LINK_PATH}#{token}"),
            expires_at=enrollment_link.expires_at,
        )

    async def find_usable_link(self, token: SecretStr, *, lock: bool) -> AccountPasskeyEnrollmentLink:
        token_text = token.get_secret_value()
        enrollment_link = (
            await self.repository.find_passkey_enrollment_link(hash_secret_token(token_text), lock=lock)
            if PASSKEY_ENROLLMENT_TOKEN_PATTERN.fullmatch(token_text)
            else None
        )
        if enrollment_link is None:
            raise PasskeyEnrollmentLinkUnknownError(
                "this passkey link is not valid; check it was copied whole, or ask for a new one"
            )
        if enrollment_link.used_at is not None:
            raise PasskeyEnrollmentLinkUsedError("this passkey link was already used; ask for a new one")
        if enrollment_link.expires_at <= datetime.now(UTC):
            raise PasskeyEnrollmentLinkExpiredError("this passkey link has expired; ask for a new one")
        return enrollment_link

    async def create_enrollment_options(self, token: SecretStr) -> dict[str, JsonValue]:
        """The "add a passkey" registration options for the link's account, with a challenge only this link answers."""
        try:
            enrollment_link = await self.find_usable_link(token, lock=False)
            account = await self.repository.find_account(enrollment_link.account_id)
            if account is None:
                raise PasskeyEnrollmentLinkUnknownError("this passkey link's account is gone")
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="enroll_passkey", reason=refusal.code))
            raise
        challenge = await self.account_service.store_new_challenge(
            purpose=ChallengePurpose.ENROLL_PASSKEY,
            account_id=account.id,
            requested_change_digest=enrollment_link.token_hash,
        )
        return self.account_service.build_registration_options(
            challenge=challenge,
            user_handle=account.passkey_user_handle,
            username=account.username,
            existing_passkeys=await self.repository.list_account_passkeys(account.id),
        )

    async def enroll_passkey(
        self, token: SecretStr, *, credential_json: dict[str, JsonValue], label: str
    ) -> PasskeyEnrollment:
        """Adds the passkey once its ``enroll_passkey`` challenge for this link verifies, marks the link used and
        signs the browser in. The caller records the account event in the same transaction."""
        try:
            enrollment_link = await self.find_usable_link(token, lock=True)
            challenge, challenge_row = await self.account_service.consume_signed_challenge(
                credential_json, purpose=ChallengePurpose.ENROLL_PASSKEY
            )
            if (
                challenge_row.account_id != enrollment_link.account_id
                or challenge_row.requested_change_digest != enrollment_link.token_hash
            ):
                raise PasskeyChallengeExpiredError("the passkey prompt was for another link; try again")
            account = await self.repository.find_account(enrollment_link.account_id)
            if account is None:
                raise PasskeyEnrollmentLinkUnknownError("this passkey link's account is gone")
            verified_registration = self.account_service.verify_new_passkey(credential_json, challenge=challenge)
            account_passkey = await self.account_service.insert_verified_passkey(
                verified_registration, credential_json=credential_json, account_id=account.id, label=label
            )
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="enroll_passkey", reason=refusal.code))
            raise
        await self.repository.mark_passkey_enrollment_link_used(enrollment_link.id, used_at=datetime.now(UTC))
        log.emit(
            logger,
            event=log_events.AccountPasskeyEnrolledByLink(
                account_id=account.id,
                account_passkey_id=account_passkey.id,
                passkey_enrollment_link_id=enrollment_link.id,
            ),
        )
        return PasskeyEnrollment(
            account_id=account.id,
            started_session=await self.account_service.start_account_session(account, account_passkey=account_passkey),
        )
