"""AES-256-GCM sealing of small secrets at rest (2026-10-12): the one copy of the AES-GCM code; each caller picks its
own ``info`` and associated data.

A random 96-bit nonce per seal, the caller's row binding (an account id, or more) as associated data so a sealed value
cannot be moved to another row
(https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM), under a
key derived with HKDF-SHA256 from ``PAY_INVOICE_SALT_SEED``
(https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf; RFC 5869 section 3.2: a distinct
``info`` gives an independent key from one secret). A database dump alone opens nothing.
"""

import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

NONCE_BYTES = 12


class SealedBytesUnreadableError(Exception):
    """A sealed value that does not open under this seed (the seed changed, or the row was tampered with)."""


def derive_sealing_key(invoice_salt_seed: SecretStr, *, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        bytes.fromhex(invoice_salt_seed.get_secret_value())
    )


def seal_bytes(plaintext: bytes, *, associated_data: bytes, encryption_key: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(encryption_key).encrypt(nonce, plaintext, associated_data)


def open_sealed_bytes(sealed: bytes, *, associated_data: bytes, encryption_key: bytes) -> bytes:
    try:
        return AESGCM(encryption_key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], associated_data)
    except InvalidTag as decryption_error:
        raise SealedBytesUnreadableError("the sealed value does not open under this seed") from decryption_error
