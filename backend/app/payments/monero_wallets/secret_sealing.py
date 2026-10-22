"""The private view key at rest (PLAN.md section 14): sealed like a webhook secret (``app.shared.sealed_bytes``) under
its own ``info``, with ``account_id|chain_key|primary_address`` as associated data, so a sealed view key opens only on
its own row: it cannot be moved to another account, chain or address."""

from pydantic import SecretStr

from app.shared.sealed_bytes import derive_sealing_key, open_sealed_bytes, seal_bytes

PRIVATE_VIEW_KEY_ENCRYPTION_KEY_INFO = b"pay-7331 account monero private view key encryption v1"


def derive_private_view_key_encryption_key(invoice_salt_seed: SecretStr) -> bytes:
    return derive_sealing_key(invoice_salt_seed, info=PRIVATE_VIEW_KEY_ENCRYPTION_KEY_INFO)


def build_private_view_key_associated_data(*, account_id: int, chain_key: str, primary_address: str) -> bytes:
    return f"{account_id}|{chain_key}|{primary_address}".encode()


def seal_private_view_key(
    private_view_key: SecretStr, *, account_id: int, chain_key: str, primary_address: str, encryption_key: bytes
) -> bytes:
    return seal_bytes(
        private_view_key.get_secret_value().encode(),
        associated_data=build_private_view_key_associated_data(
            account_id=account_id, chain_key=chain_key, primary_address=primary_address
        ),
        encryption_key=encryption_key,
    )


def open_private_view_key(
    sealed_private_view_key: bytes, *, account_id: int, chain_key: str, primary_address: str, encryption_key: bytes
) -> SecretStr:
    return SecretStr(
        open_sealed_bytes(
            sealed_private_view_key,
            associated_data=build_private_view_key_associated_data(
                account_id=account_id, chain_key=chain_key, primary_address=primary_address
            ),
            encryption_key=encryption_key,
        ).decode()
    )
