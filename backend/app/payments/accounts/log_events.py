"""Typed log records of accounts, passkeys and sessions. Ids only: never a username, an address, a token, a
credential or a client IP."""

import logging

from app.shared.logging import LogEvent


class AccountCreated(LogEvent):
    message = "Account created with its first passkey"
    operation = "account_sign_up"
    account_id: int
    account_passkey_id: int


class AccountPasskeyAdded(LogEvent):
    message = "Passkey added to an account"
    operation = "account_passkey_registration"
    account_id: int
    account_passkey_id: int


class AccountPasskeyDeleted(LogEvent):
    message = "Account passkey deleted; its sessions ended"
    operation = "account_passkey_deletion"
    account_id: int
    account_passkey_id: int


class AccountAcceptedAssetsSaved(LogEvent):
    message = "Account's starting coins saved"
    operation = "account_settings"
    account_id: int
    chain_keys: list[str]


class AccountConfirmationsFinalSaved(LogEvent):
    message = "Account's confirmations before an XMR payment counts final saved"
    operation = "account_settings"
    account_id: int
    chain_keys: list[str]


class AccountInvoicePaymentTermsSaved(LogEvent):
    message = "Account's per-chain payment window and accepted shortfall saved"
    operation = "account_settings"
    account_id: int
    chain_keys: list[str]


class AccountPayoutAddressChanged(LogEvent):
    message = "Account's payout address changed after a fresh passkey check"
    operation = "account_payout_address_change"
    account_id: int
    account_passkey_id: int


class AccountSupportContactSaved(LogEvent):
    message = "Account's support contact saved"
    operation = "account_settings"
    account_id: int
    # Whether one is set now (False: cleared); never the contact itself.
    support_contact_set: bool


class AccountSignedInWithPasskey(LogEvent):
    message = "Account signed in with a passkey"
    operation = "account_sign_in"
    account_id: int
    account_passkey_id: int
    account_session_id: int


class PasskeyCeremonyRefused(LogEvent):
    level = logging.WARNING
    message = "A sign-up, passkey registration, sign-in or fresh passkey check was refused"
    operation = "account_sign_in"
    ceremony: str
    # The refusal's error code (`passkey_response_invalid`, `passkey_challenge_expired`, `username_taken`, ...).
    reason: str


class AccountApiTokenCreated(LogEvent):
    message = "API token created after a fresh passkey check"
    operation = "account_api_token"
    account_id: int
    account_api_token_id: int
    account_passkey_id: int


class AccountApiTokenRevoked(LogEvent):
    message = "API token revoked"
    operation = "account_api_token"
    account_id: int
    account_api_token_id: int


class AccountApiTokenRefused(LogEvent):
    level = logging.WARNING
    message = "A bearer request carried an API token that is malformed, unknown or revoked"
    operation = "account_api_token"
    # `malformed` or `unknown` (a revoked token is unknown); never the token or its hash.
    reason: str


class AccountSignedOut(LogEvent):
    message = "Account signed out"
    operation = "account_sign_out"
    account_session_id: int


class PasskeyEnrollmentLinkMinted(LogEvent):
    message = "Operator minted a one-time passkey enrollment link"
    operation = "account_passkey_enrollment"
    account_id: int
    passkey_enrollment_link_id: int


class AccountPasskeyEnrolledByLink(LogEvent):
    message = "Passkey added to an account through a one-time enrollment link"
    operation = "account_passkey_enrollment"
    account_id: int
    account_passkey_id: int
    passkey_enrollment_link_id: int
