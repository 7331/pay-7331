"""The closed sets of the Monero wallet domain (PLAN.md section 14)."""

from enum import StrEnum


class MoneroNetwork(StrEnum):
    """Explicit in every derivation and address: a stagenet address is never read as a mainnet one."""

    MAINNET = "mainnet"
    STAGENET = "stagenet"
