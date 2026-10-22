"""The sidecar's answers, only the fields this client reads."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class MoneroWalletCheckResult(BaseModel):
    """``POST /v1/wallet-checks``: the address's checksum, length and network prefix, and ``a*G == A`` of the private
    view key against the public view key inside the address."""

    model_config = ConfigDict(frozen=True)

    address_valid: bool
    view_key_matches: bool


class MoneroSubaddressResult(BaseModel):
    """``POST /v1/subaddresses``."""

    model_config = ConfigDict(frozen=True)

    subaddress: str


class MoneroChainHealthEntry(BaseModel):
    """One chain of ``GET /healthz`` (``xmr-scanner/README.md``). The tips come from the last completed tick only and
    are null before one: ``tip_height`` from the preferred node, ``confirmed_tip_height`` the lower of that and a node
    of another operator (null when none answered). ``last_completed_tick_at`` is Unix seconds (pydantic reads it as a
    UTC datetime). ``ok``: a tick completed within 3 poll intervals and no watched wallet is unusable."""

    model_config = ConfigDict(frozen=True)

    chain_key: str
    ok: bool
    node_count: int
    healthy_operator_count: int
    tip_height: int | None
    confirmed_tip_height: int | None
    scanned_height: int | None
    last_completed_tick_at: datetime | None
    unusable_wallet_count: int
    # Since when (Unix seconds) completed ticks have seen the confirmed tip more than 3 blocks under the tip, or none.
    confirmed_tip_lagging_since: datetime | None = None


class MoneroHealthResult(BaseModel):
    """``GET /healthz``: ``{"ok", "chains": [...]}``, one entry per chain the sidecar scans."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    chains: list[MoneroChainHealthEntry]


class MoneroChainHealth(BaseModel):
    """The sidecar's ``GET /healthz`` for one chain, once it has completed a tick: the tip it follows, the confirmed
    tip final counting reads (``None`` while no second operator answered: nothing becomes final), how far its scan
    got, when its last tick completed, how many watched wallets it cannot scan and since when the confirmed tip has
    lagged the tip (``None`` while it keeps up)."""

    model_config = ConfigDict(frozen=True)

    chain_key: str
    tip_height: int
    confirmed_tip_height: int | None
    scanned_height: int | None
    last_completed_tick_at: datetime
    unusable_wallet_count: int
    healthy_operator_count: int
    confirmed_tip_lagging_since: datetime | None = None


class MoneroTransactionCheckOutcome(StrEnum):
    """What ``POST /v1/transaction-checks`` found of a txid the payer pasted for one invoice (PLAN.md section 14): it
    fetched the transaction, decoded its outputs with the invoice wallet's view key and checked it on a second
    operator. ``counted``: a ``monero_payments`` row of that txid and invoice is already in a block; ``confirming``:
    pays the invoice's subaddress, in the pool or in a block not recorded yet (the sidecar writes the row then);
    ``other_address``: nothing of it pays this subaddress; ``not_found``: no node knows the txid; ``failed``: proven
    only: a double spend was flagged, or the second operators disagree and none confirms; ``unavailable``: nothing
    was proven (no node answered, out of time, a wallet key that does not open), so the API answers 503 and the
    payer tries again."""

    COUNTED = "counted"
    CONFIRMING = "confirming"
    OTHER_ADDRESS = "other_address"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class MoneroTransactionCheck(BaseModel):
    """``POST /v1/transaction-checks`` ``{"chain_key", "txid", "invoice_id"}`` answers ``{"outcome",
    "amount_piconero", "confirmations"}``; the amount and confirmations are null where the outcome names none
    (``not_found``, ``failed``, ``other_address``), and confirmations are 0 for a pool transaction."""

    model_config = ConfigDict(frozen=True)

    outcome: MoneroTransactionCheckOutcome
    amount_piconero: int | None
    confirmations: int | None
