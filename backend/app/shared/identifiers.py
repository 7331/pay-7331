"""Random public ids: a prefix plus 22 base58 characters (about 129 bits), never sequential (PLAN.md section 7)."""

import secrets

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
RANDOM_PART_LENGTH = 22


def generate_public_id(prefix: str) -> str:
    return prefix + "".join(secrets.choice(BASE58_ALPHABET) for _ in range(RANDOM_PART_LENGTH))
