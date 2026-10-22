"""Webhook secrets at rest (2026-10-12): the API signs every delivery with the account's secret, so it must be
recoverable, unlike a token's hash. It is sealed with AES-256-GCM through ``app.shared.sealed_bytes`` (a random 96-bit
nonce per seal, the account id as associated data so a sealed secret cannot be moved to another account's row) under
a key derived with HKDF-SHA256 from ``PAY_INVOICE_SALT_SEED`` (RFC 5869 section 3.2: distinct ``info`` gives
independent keys from one secret). No new setting: that seed is already required, never rotated and kept off the
machine, and a database dump alone opens no secret. The wire format (nonce, then ciphertext and tag) is the one rows
written before the move to ``sealed_bytes`` carry.

A Discord webhook's token (2026-10-13) is sealed the same way under its own key (another ``info``): it is the only
thing that lets anyone post to the account's channel, and it must be read back to send.
"""

import base64
import secrets

from pydantic import SecretStr

from app.shared.sealed_bytes import derive_sealing_key, open_sealed_bytes, seal_bytes

WEBHOOK_SECRET_PREFIX = "whsec_"  # noqa: S105  # the format prefix, not a secret
# Standard Webhooks: symmetric secrets of 24 to 64 bytes, base64 after `whsec_`
# (https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md).
WEBHOOK_SECRET_BYTES = 32
ENCRYPTION_KEY_INFO = b"pay-7331 account webhook secret encryption v1"
DISCORD_WEBHOOK_TOKEN_ENCRYPTION_KEY_INFO = b"pay-7331 account discord webhook token encryption v1"


def generate_webhook_secret() -> SecretStr:
    return SecretStr(WEBHOOK_SECRET_PREFIX + base64.b64encode(secrets.token_bytes(WEBHOOK_SECRET_BYTES)).decode())


def derive_webhook_secret_encryption_key(invoice_salt_seed: SecretStr) -> bytes:
    return derive_sealing_key(invoice_salt_seed, info=ENCRYPTION_KEY_INFO)


def derive_discord_webhook_token_encryption_key(invoice_salt_seed: SecretStr) -> bytes:
    """The key ``seal_webhook_secret`` and ``open_webhook_secret`` take for a Discord webhook token."""
    return derive_sealing_key(invoice_salt_seed, info=DISCORD_WEBHOOK_TOKEN_ENCRYPTION_KEY_INFO)


def seal_webhook_secret(webhook_secret: SecretStr, *, account_id: int, encryption_key: bytes) -> bytes:
    return seal_bytes(
        webhook_secret.get_secret_value().encode(),
        associated_data=str(account_id).encode(),
        encryption_key=encryption_key,
    )


def open_webhook_secret(sealed_secret: bytes, *, account_id: int, encryption_key: bytes) -> SecretStr:
    """Raises ``SealedBytesUnreadableError`` when the secret does not open under this seed."""
    return SecretStr(
        open_sealed_bytes(
            sealed_secret, associated_data=str(account_id).encode(), encryption_key=encryption_key
        ).decode()
    )
