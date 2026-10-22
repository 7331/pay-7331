"""Each static deposit address's CREATE2 salt, derived under ``PAY_INVOICE_SALT_SEED`` (PLAN.md section 17).

salt = HMAC-SHA256(seed, message), with the message ``pay-7331 static deposit address v1``, a zero byte, then the
account id, the chain key and the customer reference, each as a 2-byte big-endian length and its UTF-8 bytes
(https://docs.python.org/3/library/hmac.html#hmac.digest). An invoice's message is its id, ``inv_`` and base58
(invoices/deposit_salts.py), which never starts with this domain's prefix, so a static salt and an invoice salt never
come from the same message; the length prefixes keep any two (account, chain, reference) triples apart. Like an
invoice salt it stays secret (``sweepToDestination`` is permissionless) and is stored on the row.
"""

import hmac

from pydantic import SecretStr

STATIC_DEPOSIT_SALT_DOMAIN = b"pay-7331 static deposit address v1\x00"
FIELD_LENGTH_BYTES = 2


def encode_salt_field(text: str) -> bytes:
    encoded = text.encode("utf-8")
    return len(encoded).to_bytes(FIELD_LENGTH_BYTES, "big") + encoded


def derive_static_deposit_salt(
    *, account_id: int, chain_key: str, customer_reference: str, invoice_salt_seed: SecretStr
) -> bytes:
    """The 32-byte salt of one account's customer on one chain; the seed is the 64 hex digits ``InvoiceSettings``
    checked."""
    message = (
        STATIC_DEPOSIT_SALT_DOMAIN
        + encode_salt_field(str(account_id))
        + encode_salt_field(chain_key)
        + encode_salt_field(customer_reference)
    )
    return hmac.digest(bytes.fromhex(invoice_salt_seed.get_secret_value()), message, "sha256")
