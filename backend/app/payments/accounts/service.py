"""AccountService: accounts with passkeys (WebAuthn, py_webauthn) and the sessions they open, plus
``authenticate_account``, the one check every account route runs (PLAN.md section 7).

- Sign-up is open: a username (3 to 20 of a-z, 0-9, _; stored lowercased, so unique
  case-insensitively), a payout address (checksummed, not verified) and a first passkey. The options step stores
  the asked-for username and address on the challenge row with a fresh random WebAuthn user handle; the account is made
  only when that challenge's passkey verifies, in the same transaction. More passkeys are added while signed in,
  behind a fresh passkey check (``account_passkey_service.py``).
- Discoverable credentials with user verification required, so sign-in asks for no username: authentication options
  name no credentials and the browser offers the passkeys it holds for this RP id; the credential finds the account.
- Challenges are rows in Postgres (the source of truth; Redis holds only copies), refused after
  ``CHALLENGE_LIFETIME``. The verify step deletes its row, found from the challenge inside clientDataJSON (so the client
  carries no extra id), in the request's transaction: a verify that succeeds commits the delete, so a challenge
  answers once; one that fails rolls back with its request and the row just expires.
- A sign-in mints a random session token for an HttpOnly cookie and stores only its SHA-256.
- A person's account request is let in only by the session cookie; one that changes something must carry ``Origin``
  equal to the public origin. The account's own server uses an API token on the invoice routes only
  (``api_token_service.py``).
- The payout address changes only after a fresh passkey check, so a stolen session cannot redirect
  payouts: a ``change_payout`` challenge bound to the account and to the new address, asking for one of the account's
  own passkeys with user verification, single use, ``CHALLENGE_LIFETIME``. Invoices keep the destination they were made
  with; nothing reads the payout address after an invoice exists.
- The account's saved coins, confirmation counts and support contact live in ``preferences_service.py``.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
import hashlib
import secrets
from typing import Self

from eth_utils import is_address, to_checksum_address
from pydantic import JsonValue, SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    options_to_json_dict,
    parse_authentication_credential_json,
    parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.registration.verify_registration_response import VerifiedRegistration

from app.payments.accounts import log_events
from app.payments.accounts.configuration import RELYING_PARTY_NAME, RelyingParty, read_relying_party
from app.payments.accounts.enums import ChallengePurpose
from app.payments.accounts.errors import (
    AccountSessionExpiredError,
    OriginMismatchError,
    PasskeyAlreadyRegisteredError,
    PasskeyChallengeExpiredError,
    PasskeyNotForThisAccountError,
    PasskeyResponseInvalidError,
    PasskeyUnknownError,
    UsernameTakenError,
)
from app.payments.accounts.models import Account, AccountPasskey, PasskeyChallenge
from app.payments.accounts.payout_address_checks import refuse_gateway_deposit_payout_address
from app.payments.accounts.preferences_service import AccountPreferencesService
from app.payments.accounts.repository import AccountRepository
from app.payments.accounts.types import (
    AccountAccess,
    AccountPasskeySummary,
    GatewayDepositAddressCheck,
    PayoutAddressChange,
    StartedAccountSession,
)
from app.shared import logging as log
from app.shared.errors import AppError, InvalidInputError, UnauthorizedError

logger = log.get_logger(__name__)

ACCOUNT_SESSION_LIFETIME = timedelta(days=30)
# W3C WebAuthn Level 3: the ceremony timeout's recommended default is 300,000 ms of a 300,000 to 600,000 ms range
# (section 15.1, https://www.w3.org/TR/webauthn-3/#sctn-timeout-recommended-range), and challenges "SHOULD be valid
# for a duration similar to the upper limit" of that range (section 13.4.3,
# https://www.w3.org/TR/webauthn-3/#sctn-cryptographic-challenges). The challenge outlives the browser's prompt (a
# hint), so a slow tap still verifies.
CHALLENGE_LIFETIME = timedelta(minutes=10)
CEREMONY_TIMEOUT_MILLISECONDS = 300_000
# "Verify that the credentialId is <= 1023 bytes" (W3C WebAuthn Level 3, section 7.1,
# https://www.w3.org/TR/webauthn-3/#sctn-registering-a-new-credential).
MAXIMUM_CREDENTIAL_ID_BYTES = 1023
USER_HANDLE_BYTES = 32
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
# Methods that change nothing; any other method on the cookie needs a matching Origin (CSRF, on top of SameSite).
SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def hash_secret_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_challenge(challenge: bytes) -> str:
    return hashlib.sha256(challenge).hexdigest()


def normalize_payout_address(payout_address: str) -> str:
    if not is_address(payout_address) or to_checksum_address(payout_address) == ZERO_ADDRESS:
        raise InvalidInputError("payout_address: a 0x address money can be sent to is required")
    return to_checksum_address(payout_address)


def read_signed_challenge(credential_json: dict[str, JsonValue]) -> bytes:
    """The challenge the authenticator signed, from the credential's clientDataJSON (verified again by py_webauthn).
    A ceremony run in another origin's iframe (``crossOrigin`` true) is refused: no page here is framed, and W3C
    WebAuthn Level 3 sections 7.1 and 7.2 accept one only where the Relying Party expects it
    (https://www.w3.org/TR/webauthn-3/#sctn-verifying-assertion). py_webauthn 3.0.1 parses ``crossOrigin`` but does not
    check it. A ``topOrigin`` comes only with ``crossOrigin`` true, so it is refused with it."""
    response = credential_json.get("response")
    client_data_text = response.get("clientDataJSON") if isinstance(response, dict) else None
    if not isinstance(client_data_text, str):
        raise PasskeyResponseInvalidError("the credential has no clientDataJSON")
    try:
        client_data = parse_client_data_json(base64url_to_bytes(client_data_text))
    except (WebAuthnException, ValueError) as parse_error:
        raise PasskeyResponseInvalidError("the credential's clientDataJSON is unreadable") from parse_error
    if client_data.cross_origin is True:
        raise PasskeyResponseInvalidError("the passkey prompt ran inside another site's page")
    return client_data.challenge


def present_account_passkey(
    account_passkey: AccountPasskey, *, current_session_passkey_id: int | None
) -> AccountPasskeySummary:
    return AccountPasskeySummary(
        account_passkey_public_id=account_passkey.public_id,
        label=account_passkey.label,
        created_at=account_passkey.created_at,
        last_used_at=account_passkey.last_used_at,
        used_by_current_session=account_passkey.id == current_session_passkey_id,
    )


def known_transports(transport_names: list[str]) -> list[AuthenticatorTransport]:
    known_values = {transport.value for transport in AuthenticatorTransport}
    return [AuthenticatorTransport(name) for name in transport_names if name in known_values]


class AccountService:
    def __init__(
        self,
        *,
        repository: AccountRepository,
        relying_party: RelyingParty,
        account_preferences_service: AccountPreferencesService,
    ) -> None:
        self.repository = repository
        self.relying_party = relying_party
        self.account_preferences_service = account_preferences_service

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(
            repository=AccountRepository(session=session),
            relying_party=read_relying_party(),
            account_preferences_service=AccountPreferencesService.on_session(session),
        )

    # ---- Challenges ----

    async def store_new_challenge(
        self,
        *,
        purpose: ChallengePurpose,
        account_id: int | None = None,
        sign_up_username: str | None = None,
        sign_up_payout_address: str | None = None,
        sign_up_user_handle: bytes | None = None,
        requested_payout_address: str | None = None,
        requested_change_digest: str | None = None,
    ) -> bytes:
        now = datetime.now(UTC)
        await self.repository.delete_expired_challenges(now=now)
        challenge = secrets.token_bytes(32)
        await self.repository.insert_challenge(
            PasskeyChallenge(
                challenge_hash=hash_challenge(challenge),
                purpose=purpose.value,
                expires_at=now + CHALLENGE_LIFETIME,
                account_id=account_id,
                sign_up_username=sign_up_username,
                sign_up_payout_address=sign_up_payout_address,
                sign_up_user_handle=sign_up_user_handle,
                requested_payout_address=requested_payout_address,
                requested_change_digest=requested_change_digest,
            )
        )
        return challenge

    async def consume_signed_challenge(
        self, credential_json: dict[str, JsonValue], *, purpose: ChallengePurpose
    ) -> tuple[bytes, PasskeyChallenge]:
        challenge = read_signed_challenge(credential_json)
        challenge_row = await self.repository.consume_unexpired_challenge(
            hash_challenge(challenge), purpose=purpose.value, now=datetime.now(UTC)
        )
        if challenge_row is None:
            raise PasskeyChallengeExpiredError("the passkey prompt expired or was already used; try again")
        return challenge, challenge_row

    def verify_new_passkey(self, credential_json: dict[str, JsonValue], *, challenge: bytes) -> VerifiedRegistration:
        try:
            verified_registration = verify_registration_response(
                credential=parse_registration_credential_json(credential_json),
                expected_challenge=challenge,
                expected_rp_id=self.relying_party.relying_party_id,
                expected_origin=self.relying_party.expected_origin,
                require_user_verification=True,
            )
        except WebAuthnException as verification_error:
            raise PasskeyResponseInvalidError(
                f"the passkey did not verify: {verification_error}"
            ) from verification_error
        if len(verified_registration.credential_id) > MAXIMUM_CREDENTIAL_ID_BYTES:
            raise PasskeyResponseInvalidError("the passkey's credential id is longer than 1023 bytes")
        return verified_registration

    async def insert_verified_passkey(
        self,
        verified_registration: VerifiedRegistration,
        *,
        credential_json: dict[str, JsonValue],
        account_id: int,
        label: str,
    ) -> AccountPasskey:
        if await self.repository.find_passkey_by_credential_id(verified_registration.credential_id) is not None:
            raise PasskeyAlreadyRegisteredError("this passkey is already registered")
        transports = parse_registration_credential_json(credential_json).response.transports or []
        try:
            return await self.repository.insert_passkey(
                AccountPasskey(
                    account_id=account_id,
                    credential_id=verified_registration.credential_id,
                    public_key=verified_registration.credential_public_key,
                    sign_count=verified_registration.sign_count,
                    transports=[transport.value for transport in transports],
                    label=label.strip(),
                )
            )
        except IntegrityError as duplicate_error:
            raise PasskeyAlreadyRegisteredError("this passkey is already registered") from duplicate_error

    def build_registration_options(
        self,
        *,
        challenge: bytes,
        user_handle: bytes,
        username: str,
        existing_passkeys: list[AccountPasskey],
    ) -> dict[str, JsonValue]:
        options = generate_registration_options(
            rp_id=self.relying_party.relying_party_id,
            rp_name=RELYING_PARTY_NAME,
            user_id=user_handle,
            user_name=username,
            user_display_name=username,
            challenge=challenge,
            timeout=CEREMONY_TIMEOUT_MILLISECONDS,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(
                    id=account_passkey.credential_id, transports=known_transports(account_passkey.transports)
                )
                for account_passkey in existing_passkeys
            ],
        )
        return options_to_json_dict(options)

    # ---- Sign-up (public) ----

    async def create_sign_up_options(
        self,
        *,
        username: str,
        payout_address: str,
        gateway_deposit_address_check: GatewayDepositAddressCheck,
    ) -> dict[str, JsonValue]:
        normalized_username = username.lower()
        checksummed_payout_address = normalize_payout_address(payout_address)
        await refuse_gateway_deposit_payout_address(
            self.repository.session,
            payout_address=checksummed_payout_address,
            gateway_deposit_address_check=gateway_deposit_address_check,
        )
        if await self.repository.username_exists(normalized_username):
            log.emit(
                logger, event=log_events.PasskeyCeremonyRefused(ceremony="sign_up", reason=UsernameTakenError.code)
            )
            raise UsernameTakenError("that username is taken; pick another")
        user_handle = secrets.token_bytes(USER_HANDLE_BYTES)
        challenge = await self.store_new_challenge(
            purpose=ChallengePurpose.SIGN_UP,
            sign_up_username=normalized_username,
            sign_up_payout_address=checksummed_payout_address,
            sign_up_user_handle=user_handle,
        )
        return self.build_registration_options(
            challenge=challenge, user_handle=user_handle, username=normalized_username, existing_passkeys=[]
        )

    async def sign_up(self, credential_json: dict[str, JsonValue], *, label: str) -> StartedAccountSession:
        """Makes the account the sign-up challenge asked for, with this passkey, and signs it in."""
        try:
            challenge, challenge_row = await self.consume_signed_challenge(
                credential_json, purpose=ChallengePurpose.SIGN_UP
            )
            verified_registration = self.verify_new_passkey(credential_json, challenge=challenge)
            if (
                challenge_row.sign_up_username is None
                or challenge_row.sign_up_payout_address is None
                or challenge_row.sign_up_user_handle is None
            ):
                raise PasskeyChallengeExpiredError("the sign-up prompt is incomplete; start again")
            if await self.repository.username_exists(challenge_row.sign_up_username):
                raise UsernameTakenError("that username was just taken; pick another")
            try:
                account = await self.repository.insert_account(
                    Account(
                        username=challenge_row.sign_up_username,
                        payout_address=challenge_row.sign_up_payout_address,
                        passkey_user_handle=challenge_row.sign_up_user_handle,
                    )
                )
            except IntegrityError as duplicate_error:
                raise UsernameTakenError("that username was just taken; pick another") from duplicate_error
            account_passkey = await self.insert_verified_passkey(
                verified_registration, credential_json=credential_json, account_id=account.id, label=label
            )
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="sign_up", reason=refusal.code))
            raise
        log.emit(logger, event=log_events.AccountCreated(account_id=account.id, account_passkey_id=account_passkey.id))
        return await self.start_account_session(account, account_passkey=account_passkey)

    # ---- Fresh passkey checks: passkeys, payout address change, API tokens and webhooks ----

    async def create_fresh_passkey_check_options(
        self,
        account_access: AccountAccess,
        *,
        purpose: ChallengePurpose,
        requested_payout_address: str | None = None,
        requested_change_digest: str | None = None,
    ) -> dict[str, JsonValue]:
        """WebAuthn request options for a fresh passkey check of the signed-in account: a new single-use challenge
        bound to this account and to ``purpose`` (and, for a payout change, the address; for a Monero wallet change
        or a passkey deletion, the digest of what it approves), only this account's passkeys allowed, user
        verification required."""
        account_passkeys = await self.repository.list_account_passkeys(account_access.account_id)
        challenge = await self.store_new_challenge(
            purpose=purpose,
            account_id=account_access.account_id,
            requested_payout_address=requested_payout_address,
            requested_change_digest=requested_change_digest,
        )
        options = generate_authentication_options(
            rp_id=self.relying_party.relying_party_id,
            challenge=challenge,
            timeout=CEREMONY_TIMEOUT_MILLISECONDS,
            allow_credentials=[
                PublicKeyCredentialDescriptor(
                    id=account_passkey.credential_id, transports=known_transports(account_passkey.transports)
                )
                for account_passkey in account_passkeys
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return options_to_json_dict(options)

    async def verify_fresh_passkey_check(
        self,
        account_access: AccountAccess,
        *,
        purpose: ChallengePurpose,
        credential_json: dict[str, JsonValue],
        requested_payout_address: str | None = None,
        requested_change_digest: str | None = None,
    ) -> tuple[Account, AccountPasskey]:
        """Verifies the assertion (W3C WebAuthn Level 3, section 7.2) against an unexpired challenge of ``purpose``
        made for this account and for exactly ``requested_payout_address`` and ``requested_change_digest`` (None on
        the purposes that bind neither), signed by one of this account's passkeys with user verification. The account
        row comes back locked (``FOR NO KEY UPDATE``).

        Once the assertion verifies, the challenge is deleted and the passkey's use recorded in a transaction of
        their own, committed at once: a refusal later in the request rolls back the change it was for, never the
        consumption, so an assertion answers once whatever happens after it. A refusal up to here consumes nothing
        (the row just expires); callers run every check that can refuse without a race before this one."""
        challenge = read_signed_challenge(credential_json)
        challenge_row = await self.repository.find_unexpired_challenge(
            hash_challenge(challenge), purpose=purpose.value, now=datetime.now(UTC)
        )
        if challenge_row is None:
            raise PasskeyChallengeExpiredError("the passkey prompt expired or was already used; try again")
        if challenge_row.account_id != account_access.account_id:
            raise PasskeyChallengeExpiredError("the passkey check was asked for another sign-in; try again")
        if challenge_row.requested_payout_address != requested_payout_address:
            raise PasskeyChallengeExpiredError("the passkey check was for another address; try again")
        if challenge_row.requested_change_digest != requested_change_digest:
            raise PasskeyChallengeExpiredError("the passkey check was for another change; try again")
        try:
            credential = parse_authentication_credential_json(credential_json)
        except WebAuthnException as parse_error:
            raise PasskeyResponseInvalidError(f"the passkey response is malformed: {parse_error}") from parse_error
        account = await self.repository.find_account_for_update(account_access.account_id)
        if account is None:
            raise AccountSessionExpiredError("the account is gone; sign up again")
        account_passkey = await self.repository.find_passkey_by_credential_id(credential.raw_id)
        # The account was identified before the ceremony (the session), so the credential must be one of its own,
        # and a userHandle, when sent, must be its handle (W3C WebAuthn Level 3, section 7.2, step 6).
        if (
            account_passkey is None
            or account_passkey.account_id != account.id
            or (
                credential.response.user_handle is not None
                and credential.response.user_handle != account.passkey_user_handle
            )
        ):
            raise PasskeyNotForThisAccountError("confirm with a passkey of this account")
        try:
            verified_authentication = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self.relying_party.relying_party_id,
                expected_origin=self.relying_party.expected_origin,
                credential_public_key=account_passkey.public_key,
                credential_current_sign_count=account_passkey.sign_count,
                require_user_verification=True,
            )
        except WebAuthnException as verification_error:
            raise PasskeyResponseInvalidError(
                f"the passkey did not verify: {verification_error}"
            ) from verification_error
        if not await self.repository.consume_challenge_and_record_passkey_use_committed(
            challenge_row.id,
            account_passkey_id=account_passkey.id,
            sign_count=verified_authentication.new_sign_count,
            used_at=datetime.now(UTC),
        ):
            raise PasskeyChallengeExpiredError("the passkey prompt expired or was already used; try again")
        return account, account_passkey

    async def create_payout_address_change_options(
        self,
        account_access: AccountAccess,
        *,
        payout_address: str,
        gateway_deposit_address_check: GatewayDepositAddressCheck,
    ) -> dict[str, JsonValue]:
        """Fresh passkey check options approving ``payout_address`` (checked as at sign-up, refused if unchanged)."""
        requested_payout_address = normalize_payout_address(payout_address)
        if requested_payout_address == account_access.payout_address:
            raise InvalidInputError("payout_address: that is already your payout address")
        await refuse_gateway_deposit_payout_address(
            self.repository.session,
            payout_address=requested_payout_address,
            gateway_deposit_address_check=gateway_deposit_address_check,
        )
        return await self.create_fresh_passkey_check_options(
            account_access,
            purpose=ChallengePurpose.CHANGE_PAYOUT_ADDRESS,
            requested_payout_address=requested_payout_address,
        )

    async def change_payout_address(
        self,
        account_access: AccountAccess,
        *,
        payout_address: str,
        credential_json: dict[str, JsonValue],
        gateway_deposit_address_check: GatewayDepositAddressCheck,
    ) -> PayoutAddressChange:
        """Changes the payout address once a ``change_payout`` passkey check made for this account and this address
        verifies (``verify_fresh_passkey_check``)."""
        try:
            requested_payout_address = normalize_payout_address(payout_address)
            # Before the passkey check, so the one refusal left after it is a change that landed meanwhile.
            if account_access.payout_address == requested_payout_address:
                raise InvalidInputError("payout_address: that is already your payout address")
            await refuse_gateway_deposit_payout_address(
                self.repository.session,
                payout_address=requested_payout_address,
                gateway_deposit_address_check=gateway_deposit_address_check,
            )
            account, account_passkey = await self.verify_fresh_passkey_check(
                account_access,
                purpose=ChallengePurpose.CHANGE_PAYOUT_ADDRESS,
                credential_json=credential_json,
                requested_payout_address=requested_payout_address,
            )
            if account.payout_address == requested_payout_address:
                raise InvalidInputError("payout_address: that is already your payout address")
        except AppError as refusal:
            log.emit(
                logger, event=log_events.PasskeyCeremonyRefused(ceremony="change_payout_address", reason=refusal.code)
            )
            raise
        previous_payout_address = account.payout_address
        await self.repository.update_payout_address(account.id, payout_address=requested_payout_address)
        log.emit(
            logger,
            event=log_events.AccountPayoutAddressChanged(account_id=account.id, account_passkey_id=account_passkey.id),
        )
        return PayoutAddressChange(
            previous_payout_address=previous_payout_address, payout_address=requested_payout_address
        )

    # ---- The operator's account (2026-10-12) ----

    async def find_operator_account_id(self, operator_alert_account_id: int | None) -> int | None:
        """The account operator alerts go to: the first ``PAY_OPERATOR_ACCOUNT_IDS`` names, pinned by id because payout
        addresses are not verified and anyone can sign up with the operator's. None when the list is empty or its first
        id names no account."""
        if operator_alert_account_id is None:
            return None
        account = await self.repository.find_account(operator_alert_account_id)
        return account.id if account is not None else None

    async def list_missing_account_ids(self, account_ids: Sequence[int]) -> list[int]:
        """The given ids no account has, in the given order."""
        existing_account_ids = await self.repository.list_existing_account_ids(account_ids)
        return [account_id for account_id in account_ids if account_id not in existing_account_ids]

    # ---- Sign-in (public) ----

    async def create_sign_in_options(self) -> dict[str, JsonValue]:
        challenge = await self.store_new_challenge(purpose=ChallengePurpose.SIGN_IN)
        options = generate_authentication_options(
            rp_id=self.relying_party.relying_party_id,
            challenge=challenge,
            timeout=CEREMONY_TIMEOUT_MILLISECONDS,
            allow_credentials=[],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return options_to_json_dict(options)

    async def sign_in_with_passkey(self, credential_json: dict[str, JsonValue]) -> StartedAccountSession:
        try:
            challenge, _challenge_row = await self.consume_signed_challenge(
                credential_json, purpose=ChallengePurpose.SIGN_IN
            )
            try:
                credential = parse_authentication_credential_json(credential_json)
            except WebAuthnException as parse_error:
                raise PasskeyResponseInvalidError(f"the passkey response is malformed: {parse_error}") from parse_error
            account_passkey = await self.repository.find_passkey_by_credential_id(credential.raw_id)
            if account_passkey is None:
                raise PasskeyUnknownError("this passkey is not registered here (or was deleted)")
            try:
                verified_authentication = verify_authentication_response(
                    credential=credential,
                    expected_challenge=challenge,
                    expected_rp_id=self.relying_party.relying_party_id,
                    expected_origin=self.relying_party.expected_origin,
                    credential_public_key=account_passkey.public_key,
                    credential_current_sign_count=account_passkey.sign_count,
                    require_user_verification=True,
                )
            except WebAuthnException as verification_error:
                raise PasskeyResponseInvalidError(
                    f"the passkey did not verify: {verification_error}"
                ) from verification_error
            account = await self.repository.find_account(account_passkey.account_id)
            if account is None:
                raise PasskeyUnknownError("this passkey's account is gone")
            # Nobody was named before a discoverable sign-in, so the userHandle must be present and be the account's
            # handle (W3C WebAuthn Level 3, section 7.2, https://www.w3.org/TR/webauthn-3/#sctn-verifying-assertion).
            if credential.response.user_handle != account.passkey_user_handle:
                raise PasskeyResponseInvalidError("the passkey's user handle is not its account's")
        except AppError as refusal:
            log.emit(logger, event=log_events.PasskeyCeremonyRefused(ceremony="sign_in", reason=refusal.code))
            raise
        await self.repository.record_passkey_use(
            account_passkey.id, sign_count=verified_authentication.new_sign_count, used_at=datetime.now(UTC)
        )
        return await self.start_account_session(account, account_passkey=account_passkey)

    async def start_account_session(
        self, account: Account, *, account_passkey: AccountPasskey
    ) -> StartedAccountSession:
        now = datetime.now(UTC)
        await self.repository.delete_expired_sessions(now=now)
        session_token = secrets.token_urlsafe(32)
        account_session = await self.repository.insert_session(
            hash_secret_token(session_token),
            account_passkey_id=account_passkey.id,
            expires_at=now + ACCOUNT_SESSION_LIFETIME,
        )
        log.emit(
            logger,
            event=log_events.AccountSignedInWithPasskey(
                account_id=account.id, account_passkey_id=account_passkey.id, account_session_id=account_session.id
            ),
        )
        account_preferences_service = self.account_preferences_service
        return StartedAccountSession(
            account_session_id=account_session.id,
            session_token=SecretStr(session_token),
            username=account.username,
            payout_address=account.payout_address,
            accepted_asset_symbols_by_chain=await account_preferences_service.resolve_accepted_asset_symbols_by_chain(
                account.id, saved_by_chain=account.accepted_asset_symbols_by_chain
            ),
            confirmations_final_by_chain=account_preferences_service.resolve_confirmations_final_by_chain(
                saved_by_chain=account.confirmations_final_by_chain
            ),
            invoice_lifetime_seconds_by_chain=account_preferences_service.resolve_saved_values_of_enabled_chains(
                account.invoice_lifetime_seconds_by_chain
            ),
            accuracy_percent_by_chain=account_preferences_service.resolve_saved_values_of_enabled_chains(
                account.accuracy_percent_by_chain
            ),
            support_contact=account.support_contact,
        )

    async def sign_out(self, session_token: SecretStr) -> None:
        account_session_id = await self.repository.delete_session_by_token_hash(
            hash_secret_token(session_token.get_secret_value())
        )
        if account_session_id is not None:
            log.emit(logger, event=log_events.AccountSignedOut(account_session_id=account_session_id))

    # ---- The account check ----

    async def authenticate_account(
        self, session_token: SecretStr | None, *, request_method: str, origin_header: str | None
    ) -> AccountAccess:
        """The session cookie's unexpired session (an Authorization header counts for nothing); a request that changes
        something needs ``Origin`` equal to the public origin."""
        if session_token is None:
            raise UnauthorizedError("sign in with your passkey")
        found = await self.repository.find_unexpired_session(
            hash_secret_token(session_token.get_secret_value()), now=datetime.now(UTC)
        )
        if found is None:
            raise AccountSessionExpiredError("the sign-in expired or was ended; sign in with your passkey again")
        if request_method.upper() not in SAFE_HTTP_METHODS and (
            origin_header is None or origin_header.lower() != self.relying_party.expected_origin
        ):
            raise OriginMismatchError("a signed-in change must come from this site's own pages")
        account_session, account_passkey, account = found
        account_preferences_service = self.account_preferences_service
        return AccountAccess(
            account_id=account.id,
            username=account.username,
            payout_address=account.payout_address,
            account_session_id=account_session.id,
            account_passkey_id=account_passkey.id,
            accepted_asset_symbols_by_chain=await account_preferences_service.resolve_accepted_asset_symbols_by_chain(
                account.id, saved_by_chain=account.accepted_asset_symbols_by_chain
            ),
            confirmations_final_by_chain=account_preferences_service.resolve_confirmations_final_by_chain(
                saved_by_chain=account.confirmations_final_by_chain
            ),
            invoice_lifetime_seconds_by_chain=account_preferences_service.resolve_saved_values_of_enabled_chains(
                account.invoice_lifetime_seconds_by_chain
            ),
            accuracy_percent_by_chain=account_preferences_service.resolve_saved_values_of_enabled_chains(
                account.accuracy_percent_by_chain
            ),
            support_contact=account.support_contact,
        )

    async def find_live_session_account_ids(self, account_session_ids: list[int]) -> dict[int, int]:
        """``authenticate_account``'s session check for many sessions at once, by session id (the account event
        stream's shared poller): account id by session id, for those still unexpired. A sign-out or a deleted passkey
        deletes the session, so it is missing here."""
        if not account_session_ids:
            return {}
        return await self.repository.list_unexpired_session_account_ids(account_session_ids, now=datetime.now(UTC))

    async def find_account_access(
        self, session_token: SecretStr | None, *, request_method: str, origin_header: str | None
    ) -> AccountAccess | None:
        """``authenticate_account``, except that no cookie or a dead session is just signed out (None); a live
        session from another origin is still refused."""
        try:
            return await self.authenticate_account(
                session_token, request_method=request_method, origin_header=origin_header
            )
        except AccountSessionExpiredError:
            return None
        except UnauthorizedError:
            if session_token is None:
                return None
            raise
