"""Typed log records of the Monero wallet domain. Ids and counts only: never an address, a key or a username."""

import logging

from app.shared.logging import LogEvent


class MoneroWalletSet(LogEvent):
    message = "Monero wallet set"
    operation = "monero_wallet"
    account_id: int
    chain_key: str
    wallet_id: int
    replaced_wallet_id: int | None
    # True when the account went back to an address it held before: its old row, with its minors, is active again.
    reactivated: bool


class MoneroSubaddressAllocated(LogEvent):
    message = "Monero subaddress minor allocated"
    operation = "monero_subaddress"
    wallet_id: int
    subaddress_minor: int


class MoneroSubaddressRefused(LogEvent):
    level = logging.ERROR
    message = "The Monero sidecar answered a subaddress of the wrong length or network; the invoice was refused"
    operation = "monero_subaddress"
    wallet_id: int


class MoneroLookaheadWarning(LogEvent):
    level = logging.WARNING
    message = "A Monero wallet's gap of subaddresses past its last received output reached the lookahead warning"
    operation = "monero_subaddress"
    wallet_id: int
    unreceived_minor_gap: int
    confirmed_lookahead_minor_count: int


class MoneroLookaheadRefused(LogEvent):
    level = logging.WARNING
    message = "A new Monero invoice was refused: the gap past the last received output reached the lookahead limit"
    operation = "monero_subaddress"
    wallet_id: int
    unreceived_minor_gap: int
    confirmed_lookahead_minor_count: int


class MoneroLookaheadConfirmed(LogEvent):
    message = "A receiver confirmed a wider Monero wallet lookahead"
    operation = "monero_subaddress"
    wallet_id: int
    confirmed_lookahead_minor_count: int
