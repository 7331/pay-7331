"""AccountPasskeyService: the signed-in account's own passkeys in ``/settings``: list, rename, add and delete (PLAN.md
section 7).

Adding and deleting a passkey each need a fresh passkey check (``AccountService.verify_fresh_passkey_check``), so a
stolen session cookie cannot add the thief's passkey, nor delete the owner's and with it the owner's sessions. Either
would make the session a lasting takeover: every later fresh check would accept the thief's passkey. OWASP asks for
re-authentication with an authenticator already bound to the account before adding a trusted device
(https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html).

- Adding: an ``approve_passkey`` check bound to the account; once it verifies, the registration options carry an
  ``add_passkey`` challenge bound to the account, single use, ``CHALLENGE_LIFETIME``. No other step makes one.
- Deleting: a ``delete_passkey`` check bound to the account and to the passkey it deletes (SHA-256 of its internal
  id, in ``requested_change_digest``), so an assertion deletes only that one. The last passkey stays. The routes name
  a passkey by its public id (``pk_...``), resolved among the account's own passkeys only.
- The account events (``account.passkey_added``, ``account.passkey_deleted``) are recorded by
  ``AccountEventService`` in the same transaction.
- Renaming changes only a label and needs the session alone.
"""

import hashlib
from typing import Self

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.accounts import log_events
from app.payments.accounts.enums import ChallengePurpose
from app.payments.accounts.errors import (
    AccountPasskeyNotFoundError,
    LastPasskeyError,
    PasskeyChallengeExpiredError,
)
from app.payments.accounts.repository import AccountRepository
from app.payments.accounts.service import AccountService, present_account_passkey
from app.payments.accounts.types import AccountAccess, AccountPasskeySummary
from app.shared import logging as log
from app.shared.errors import AppError

logger = log.get_logger(__name__)


def hash_passkey_deletion(account_passkey_id: int) -> str:
    """What a ``delete_passkey`` challenge approves: SHA-256 hex of ``account_passkey|<id>``."""
    return hashlib.sha256(f"account_passkey|{account_passkey_id}".encode()).hexdigest()


class AccountPasskeyService:
    def __init__(self, *, repository: AccountRepository, account_service: AccountService) -> None:
        self.repository = repository
        self.account_service = account_service

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(repository=AccountRepository(session=session), account_service=AccountService.on_session(session))

    # ---- List and rename (session only) ----

    async def list_passkeys(self, account_access: AccountAccess) -> list[AccountPasskeySummary]:
        return [
            present_account_passkey(account_passkey, current_session_passkey_id=account_access.account_passkey_id)
            for account_passkey in await self.repository.list_account_passkeys(account_access.account_id)
        ]

    async def rename_passkey(
        self, account_access: AccountAccess, *, account_passkey_public_id: str, label: str
    ) -> AccountPasskeySummary:
        account_passkey = await self.repository.update_account_passkey_label(
            account_passkey_public_id, account_id=account_access.account_id, label=label.strip()
        )
        if account_passkey is None:
            raise AccountPasskeyNotFoundError(f"no passkey {account_passkey_public_id} on this account")
        return present_account_passkey(account_passkey, current_session_passkey_id=account_access.account_passkey_id)

    # ---- Another passkey, behind a fresh passkey check ----

    async def create_add_passkey_approval_options(self, account_access: AccountAccess) -> dict[str, JsonValue]:
        """Fresh passkey check options approving a new passkey for this account."""
        return await self.account_service.create_fresh_passkey_check_options(
            account_access, purpose=ChallengePurpose.APPROVE_ADD_PASSKEY
        )

    async def create_add_passkey_options(
        self, account_access: AccountAccess, *, approval_credential_json: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """Registration options for one more passkey, once the ``approve_passkey`` check verifies; their ``add_passkey``
        challenge is the only way to ``add_passkey``."""
        try:
            account, _approving_account_passkey = await self.account_service.verify_fresh_passkey_check(
                account_access,
                purpose=ChallengePurpose.APPROVE_ADD_PASSKEY,
                credential_json=approval_credential_json,
            )
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="approve_passkey", reason=refusal.code))
            raise
        challenge = await self.account_service.store_new_challenge(
            purpose=ChallengePurpose.ADD_PASSKEY, account_id=account.id
        )
        return self.account_service.build_registration_options(
            challenge=challenge,
            user_handle=account.passkey_user_handle,
            username=account.username,
            existing_passkeys=await self.repository.list_account_passkeys(account.id),
        )

    async def add_passkey(
        self, account_access: AccountAccess, *, credential_json: dict[str, JsonValue], label: str
    ) -> AccountPasskeySummary:
        """Adds the passkey made for an ``add_passkey`` challenge of this account. The caller records the account
        event in the same transaction."""
        try:
            challenge, challenge_row = await self.account_service.consume_signed_challenge(
                credential_json, purpose=ChallengePurpose.ADD_PASSKEY
            )
            if challenge_row.account_id != account_access.account_id:
                raise PasskeyChallengeExpiredError("the passkey prompt was for another sign-in; try again")
            verified_registration = self.account_service.verify_new_passkey(credential_json, challenge=challenge)
            account_passkey = await self.account_service.insert_verified_passkey(
                verified_registration,
                credential_json=credential_json,
                account_id=account_access.account_id,
                label=label,
            )
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="add_passkey", reason=refusal.code))
            raise
        log.emit(
            logger,
            event=log_events.AccountPasskeyAdded(
                account_id=account_access.account_id, account_passkey_id=account_passkey.id
            ),
        )
        return present_account_passkey(account_passkey, current_session_passkey_id=account_access.account_passkey_id)

    # ---- Deleting a passkey, behind a fresh passkey check ----

    async def refuse_passkey_deletion(self, account_access: AccountAccess, *, account_passkey_public_id: str) -> int:
        """The internal id of the account's passkey with that public id; refuses one this account does not hold, and
        its last one (no way to sign in would be left)."""
        account_passkeys = await self.repository.list_account_passkeys(account_access.account_id)
        account_passkey_id = next(
            (
                account_passkey.id
                for account_passkey in account_passkeys
                if account_passkey.public_id == account_passkey_public_id
            ),
            None,
        )
        if account_passkey_id is None:
            raise AccountPasskeyNotFoundError(f"no passkey {account_passkey_public_id} on this account")
        if len(account_passkeys) <= 1:
            raise LastPasskeyError("this is the account's only passkey; add another before deleting it")
        return account_passkey_id

    async def create_delete_passkey_options(
        self, account_access: AccountAccess, *, account_passkey_public_id: str
    ) -> dict[str, JsonValue]:
        """Fresh passkey check options approving the deletion of this one passkey; refused up front for one the
        account does not hold and for its last."""
        try:
            account_passkey_id = await self.refuse_passkey_deletion(
                account_access, account_passkey_public_id=account_passkey_public_id
            )
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="delete_passkey", reason=refusal.code))
            raise
        return await self.account_service.create_fresh_passkey_check_options(
            account_access,
            purpose=ChallengePurpose.DELETE_PASSKEY,
            requested_change_digest=hash_passkey_deletion(account_passkey_id),
        )

    async def delete_passkey(
        self, account_access: AccountAccess, *, account_passkey_public_id: str, credential_json: dict[str, JsonValue]
    ) -> None:
        """Deletes the passkey (its sessions end with it) once a ``delete_passkey`` check made for this account and
        this passkey verifies. Every refusal runs before the check, and again after it under the account row lock
        the check takes, so two deletions at once never leave the account without a passkey. The caller records the
        account event in the same transaction."""
        try:
            account_passkey_id = await self.refuse_passkey_deletion(
                account_access, account_passkey_public_id=account_passkey_public_id
            )
            await self.account_service.verify_fresh_passkey_check(
                account_access,
                purpose=ChallengePurpose.DELETE_PASSKEY,
                credential_json=credential_json,
                requested_change_digest=hash_passkey_deletion(account_passkey_id),
            )
            await self.refuse_passkey_deletion(account_access, account_passkey_public_id=account_passkey_public_id)
            if not await self.repository.delete_account_passkey(
                account_passkey_id, account_id=account_access.account_id
            ):
                raise AccountPasskeyNotFoundError(f"no passkey {account_passkey_public_id} on this account")
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="delete_passkey", reason=refusal.code))
            raise
        log.emit(
            logger,
            event=log_events.AccountPasskeyDeleted(
                account_id=account_access.account_id, account_passkey_id=account_passkey_id
            ),
        )
