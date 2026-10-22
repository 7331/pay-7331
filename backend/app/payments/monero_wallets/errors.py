"""Monero wallet refusals callers tell apart."""

from app.shared.errors import ConflictError, InvalidInputError, ServiceUnavailableError


class MoneroAddressInvalidError(InvalidInputError):
    """Not a standard primary address of the chain's network (checksum, length or network prefix)."""

    code = "monero_address_invalid"


class MoneroViewKeyMismatchError(InvalidInputError):
    """The private view key does not belong to the address: a ``a*G == A`` mismatch would derive subaddresses nobody
    can spend, so payers' money would burn."""

    code = "monero_view_key_mismatch"


class MoneroWalletUnchangedError(ConflictError):
    """The address is the active wallet's already (the same address means the same view key)."""

    code = "monero_wallet_unchanged"


class MoneroAddressTakenError(ConflictError):
    """Another account holds (or held) this address on this chain: its minors are that account's, and a second row would
    derive subaddresses its invoices already use."""

    code = "monero_address_taken"


class MoneroWalletChangeConflictError(ConflictError):
    """Another change of the account's Monero wallet landed at the same moment and left a different wallet active."""

    code = "monero_wallet_change_conflict"


class MoneroWalletMissingError(ConflictError):
    """XMR invoices need the account's wallet."""

    code = "monero_wallet_missing"


class MoneroLookaheadExhaustedError(ConflictError):
    """Too many subaddresses past the last one with a received output: the receiver's wallet would stop seeing the next
    payment. Refusing beats burning (PLAN.md section 14)."""

    code = "monero_lookahead_exhausted"


class MoneroSidecarUnavailableError(ServiceUnavailableError):
    """The scanner sidecar is not set up (``PAY_MONERO_SIDECAR_URL`` empty, or no Monero chain enabled) or did not
    answer: no wallet is checked or saved, no subaddress derived, no tip read and no txid checked until it does."""

    code = "monero_sidecar_unavailable"
