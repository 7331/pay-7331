"""Each invoice's CREATE2 salt, derived from its id under ``PAY_INVOICE_SALT_SEED`` (PLAN.md section 3, 2026-10-12).

salt = HMAC-SHA256(seed, invoice id): 32 bytes, the size CREATE2 takes ("always 32 bytes (a stack item)",
https://eips.ethereum.org/EIPS/eip-1014), with ``hmac.digest`` (https://docs.python.org/3/library/hmac.html#hmac.digest).
The id, the destination and the seed give the salt back, and the factory's ``predictDepositAddress`` confirms it, so a
deposit stays sweepable when Postgres is lost (``python -m app.cli recover-deposit``). The salt still stays secret,
not just the id: ``sweepToDestination`` is permissionless, and anyone holding a salt could sweep that deposit before
the watcher has read and settled it (funds would still only reach the destination). Invoices made before the seed keep
their random stored salt; every invoice row keeps its salt either way, as the backups hold it.
"""

import hmac

from pydantic import SecretStr


def derive_invoice_salt(invoice_id: str, *, invoice_salt_seed: SecretStr) -> bytes:
    """The 32-byte salt of ``invoice_id``; the seed is the 64 hex digits ``InvoiceSettings`` checked."""
    return hmac.digest(bytes.fromhex(invoice_salt_seed.get_secret_value()), invoice_id.encode("ascii"), "sha256")
