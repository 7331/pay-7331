"""What a stored WebAuthn challenge may answer."""

from enum import StrEnum


class ChallengePurpose(StrEnum):
    # A new account: the challenge row carries the username, payout address and user handle asked for.
    SIGN_UP = "sign_up"
    # Another passkey for the signed-in account the challenge row names; made only once an `approve_passkey` check of
    # that account verified.
    ADD_PASSKEY = "add_passkey"
    SIGN_IN = "sign_in"
    # A fresh passkey check of the signed-in account the row names, approving the payout address the row carries.
    # The column holds 16 characters.
    CHANGE_PAYOUT_ADDRESS = "change_payout"
    # A fresh passkey check of the signed-in account the row names, approving a new API token (2026-10-12).
    CREATE_API_TOKEN = "create_api_token"  # noqa: S105  # a purpose name, not a secret
    # A fresh passkey check approving a webhook change that sets its URL or shows a new secret (2026-10-12).
    CHANGE_WEBHOOK = "change_webhook"
    # A fresh passkey check approving a new Discord webhook for the account's invoice news (2026-10-13).
    CHANGE_DISCORD_WEBHOOK = "change_discord"
    # A fresh passkey check approving a new Monero wallet (primary address and view key) for XMR invoices
    # (2026-10-14, PLAN.md section 14).
    CHANGE_MONERO_WALLET = "change_monero"
    # A new passkey for the account an operator-minted enrollment link names (account recovery, 2026-10-15); the row
    # carries the account and, in ``requested_change_digest``, the link's token hash, so it answers only that link.
    ENROLL_PASSKEY = "enroll_passkey"
    # A fresh passkey check of the signed-in account the row names, approving one more passkey: a stolen session alone
    # cannot add the thief's.
    APPROVE_ADD_PASSKEY = "approve_passkey"
    # A fresh passkey check approving the deletion of the passkey whose digest the row carries in
    # ``requested_change_digest``: a stolen session alone cannot delete the owner's passkeys and sessions.
    DELETE_PASSKEY = "delete_passkey"
