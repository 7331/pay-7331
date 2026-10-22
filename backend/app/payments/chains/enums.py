"""The closed sets of the chains domain."""

from enum import StrEnum


class ChainKind(StrEnum):
    """``evm``: an EVM chain with a chain id, deposit contracts and sweeps. ``monero``: the receiver's own Monero
    wallet, no chain id and no browser wallet."""

    EVM = "evm"
    MONERO = "monero"
