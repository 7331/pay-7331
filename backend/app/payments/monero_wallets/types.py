"""Read types of the Monero wallet domain, and the ports the scanner sidecar fills in (PLAN.md section 14).

One client fills every port: ``app.infrastructure.monero_sidecar_api.client.MoneroSidecarApiClient`` when a Monero
chain is enabled and ``PAY_MONERO_SIDECAR_URL`` is set, else ``UnavailableMoneroSidecar``. Every port raises the
client's ``MoneroSidecarError`` when the sidecar is not set up, does not answer or answers nonsense, and
``MoneroSidecarInputRefusedError`` on a 400; callers turn both into ``MoneroSidecarUnavailableError`` (503) or a
refusal of their own. No port logs a key or a body.
"""

from datetime import datetime
from typing import ClassVar, NoReturn, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, SecretStr

from app.infrastructure.monero_sidecar_api.errors import MoneroSidecarError, MoneroSidecarFailureKind
from app.infrastructure.monero_sidecar_api.schemas import (
    MoneroChainHealth,
    MoneroTransactionCheck,
    MoneroWalletCheckResult,
)
from app.payments.monero_wallets.enums import MoneroNetwork


class MoneroKeyChecks(Protocol):
    """The checks before a wallet is saved, in one call (``POST /v1/wallet-checks``). The crypto (Ed25519 and Base58
    with Monero's checksum) lives in the sidecar, not in Python."""

    async def check_wallet(
        self, primary_address: str, *, private_view_key: SecretStr, network: MoneroNetwork
    ) -> MoneroWalletCheckResult:
        """``address_valid``: checksum, 95 characters and the network's prefix (mainnet 4, stagenet 5).
        ``view_key_matches``: ``a*G == A`` against the public view key inside the address."""
        ...


class MoneroSubaddressDeriver(Protocol):
    """``POST /v1/subaddresses`` with ``{"network", "primary_address", "private_view_key": "<64 hex>", "major_index",
    "minor_index"}`` answers ``{"subaddress"}``. ``MoneroWalletService`` opens the view key for this one call only."""

    async def derive_subaddress(
        self,
        primary_address: str,
        *,
        private_view_key: SecretStr,
        network: MoneroNetwork,
        major_index: int,
        minor_index: int,
    ) -> str: ...


class MoneroScannerReader(Protocol):
    """What the watcher, ``reconcile`` and the payer's "I paid" ask the sidecar."""

    async def read_chain_health(self, chain_key: str) -> MoneroChainHealth:
        """``GET /healthz``: the chain's ``tip_height`` and its healthy operator count."""
        ...

    async def check_transaction(self, chain_key: str, *, txid: str, invoice_id: str) -> MoneroTransactionCheck:
        """``POST /v1/transaction-checks``: the txid decoded with the invoice wallet's view key and cross-checked."""
        ...


class MoneroWalletSidecar(MoneroKeyChecks, MoneroSubaddressDeriver, Protocol):
    """What ``MoneroWalletService`` asks the sidecar: the wallet checks and the subaddress derivation."""


# Runtime checkable: ``InvoiceWiring`` (pydantic, arbitrary types) validates the field with ``isinstance``.
@runtime_checkable  # nosemgrep: runtime-checkable-needs-runtime-consumer
class MoneroSidecar(MoneroWalletSidecar, MoneroScannerReader, Protocol):
    """Every port, as the invoice wiring holds it."""


class UnavailableMoneroSidecar:
    """Stands in for the sidecar client while none is set up: every call raises ``MoneroSidecarError``
    (``not_configured``), so XMR invoices and wallet changes are ``monero_sidecar_unavailable`` and Monero ticks credit
    nothing."""

    def refuse(self) -> NoReturn:
        raise MoneroSidecarError(
            "the Monero scanner is not set up on this deployment", failure_kind=MoneroSidecarFailureKind.NOT_CONFIGURED
        )

    async def check_wallet(
        self,
        primary_address: str,  # noqa: ARG002  # the protocol's signature
        *,
        private_view_key: SecretStr,  # noqa: ARG002  # the protocol's signature
        network: MoneroNetwork,  # noqa: ARG002  # the protocol's signature
    ) -> MoneroWalletCheckResult:
        self.refuse()

    async def derive_subaddress(
        self,
        primary_address: str,  # noqa: ARG002  # the protocol's signature
        *,
        private_view_key: SecretStr,  # noqa: ARG002  # the protocol's signature
        network: MoneroNetwork,  # noqa: ARG002  # the protocol's signature
        major_index: int,  # noqa: ARG002  # the protocol's signature
        minor_index: int,  # noqa: ARG002  # the protocol's signature
    ) -> str:
        self.refuse()

    async def read_chain_health(self, chain_key: str) -> MoneroChainHealth:  # noqa: ARG002  # the protocol's signature
        self.refuse()

    async def check_transaction(
        self,
        chain_key: str,  # noqa: ARG002  # the protocol's signature
        *,
        txid: str,  # noqa: ARG002  # the protocol's signature
        invoice_id: str,  # noqa: ARG002  # the protocol's signature
    ) -> MoneroTransactionCheck:
        self.refuse()


class MoneroWalletValue(BaseModel):
    """An active wallet as the invoice flow reads it. Carries no key: ``MoneroWalletService`` opens the view key only
    for the one call that needs it."""

    model_config = ConfigDict(frozen=True)

    # wallet2 scans minors up to its highest minor with a received output plus its lookahead, less one
    # (``expand_subaddresses``: ``minor_end`` is the received-minor label count less one plus
    # ``m_subaddress_lookahead_minor``, default ``SUBADDRESS_LOOKAHEAD_MINOR`` 200;
    # https://github.com/monero-project/monero/blob/release-v0.18/src/wallet/wallet2.cpp). Confirming a wider lookahead
    # does not move that base, it only widens the window past it, so the gap counts from the highest received minor
    # and the limits follow the confirmed lookahead: the gate refuses at lookahead minus 1, and warns this many minors
    # before the confirmed lookahead (150 of 200, 950 of 1000), where `/activity` shows the exact widening line.
    default_lookahead_minor_count: ClassVar[int] = 200
    # `set subaddress-lookahead 50:1000` (PLAN.md section 14), what a confirmation in the sheet records.
    widened_lookahead_minor_count: ClassVar[int] = 1000
    lookahead_warning_margin_minor_count: ClassVar[int] = 50

    id: int
    account_id: int
    chain_key: str
    # The receiver's primary address (public): shown back in settings, an XMR invoice's destination and a Monero
    # quantity link's. The view key is never in this value.
    primary_address: str
    network: MoneroNetwork
    major_index: int
    next_minor_index: int
    confirmed_lookahead_minor_count: int
    created_at: datetime

    def unreceived_minor_gap(self, last_received_minor: int) -> int:
        """How many allocated minors lie past ``last_received_minor``: the highest minor of this wallet with an output
        seen in a block (0 when none), not the highest minor of an invoice in status ``paid``."""
        return max(self.next_minor_index - 1 - last_received_minor, 0)

    @property
    def lookahead_warning_minor_gap(self) -> int:
        return self.confirmed_lookahead_minor_count - self.lookahead_warning_margin_minor_count

    @property
    def lookahead_refusal_minor_gap(self) -> int:
        return self.confirmed_lookahead_minor_count - 1

    def is_past_lookahead_warning(self, last_received_minor: int) -> bool:
        return self.unreceived_minor_gap(last_received_minor) >= self.lookahead_warning_minor_gap

    def is_past_lookahead_refusal(self, last_received_minor: int) -> bool:
        return self.unreceived_minor_gap(last_received_minor) >= self.lookahead_refusal_minor_gap


class MoneroSubaddressAllocation(BaseModel):
    """The minor a new XMR invoice takes, with the wallet it belongs to."""

    model_config = ConfigDict(frozen=True)

    monero_wallet_id: int
    # The wallet's primary address: the invoice's destination.
    primary_address: str
    subaddress_minor: int
    # True when the gap has reached the warning: the caller raises it in `/activity`'s Needs you.
    lookahead_warning: bool
