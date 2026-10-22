"""MoneroWalletSettingsService: `/settings`' "Monero wallet" (PLAN.md sections 7 and 14, 2026-10-14).

It lives here, above monero_wallets, accounts and account_events, because the view needs this domain's read of the
highest subaddress minor with a received output (the lookahead gap), the change needs the accounts domain's fresh
passkey check, and the change records ``account.monero_wallet_set``; the same reason the payout address change lives
in account_events.

- ``read_wallet_settings``: the enabled Monero chains and the account's active wallet of one of them, with its gap.
- ``create_change_options`` and ``set_wallet``: a fresh passkey check (``change_monero``) bound to the wallet it
  approves (SHA-256 of ``chain_key|primary_address``, as the payout change binds its address), so an assertion sets
  only that wallet. ``set_wallet`` runs every refusal that needs no race first (the sidecar's checks, unchanged,
  taken), then the passkey check, which consumes its challenge in a committed step of its own, then the locked save
  (one row per address and chain) and the account event (ids only). A refusal after the check is a change that landed
  meanwhile, and the assertion is spent either way.
- ``confirm_lookahead``: session only; it can only make the account's own wallet refuse fewer invoices.
"""

import hashlib
from typing import Self

from pydantic import JsonValue, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.account_events.enums import AccountEventType
from app.payments.account_events.service import AccountEventService
from app.payments.accounts import log_events as account_log_events
from app.payments.accounts.enums import ChallengePurpose
from app.payments.accounts.service import AccountService
from app.payments.accounts.types import AccountAccess
from app.payments.chains.errors import ChainNotEnabledError
from app.payments.chains.service import ChainsService
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.schemas import (
    MoneroChainSummary,
    MoneroWallet,
    MoneroWalletSettingsResponse,
)
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.monero_wallets.enums import MoneroNetwork
from app.payments.monero_wallets.service import MoneroWalletService
from app.payments.monero_wallets.types import MoneroWalletValue
from app.shared import logging as log
from app.shared.errors import AppError

logger = log.get_logger(__name__)


def hash_monero_wallet_change(chain_key: str, *, primary_address: str) -> str:
    """What a ``change_monero`` challenge approves: SHA-256 hex of ``chain_key|primary_address``."""
    return hashlib.sha256(f"{chain_key}|{primary_address}".encode()).hexdigest()


class MoneroWalletSettingsService:
    def __init__(
        self,
        *,
        invoice_repository: InvoiceRepository,
        chains_service: ChainsService,
        monero_wallet_service: MoneroWalletService,
        account_service: AccountService,
        account_event_service: AccountEventService,
    ) -> None:
        self.invoice_repository = invoice_repository
        self.chains_service = chains_service
        self.monero_wallet_service = monero_wallet_service
        self.account_service = account_service
        self.account_event_service = account_event_service

    @classmethod
    def on_session(cls, session: AsyncSession, *, wiring: InvoiceWiring) -> Self:
        return cls(
            invoice_repository=InvoiceRepository(session=session),
            chains_service=wiring.chains_service,
            monero_wallet_service=MoneroWalletService.on_session(
                session,
                chains_service=wiring.chains_service,
                monero_sidecar=wiring.monero_sidecar,
                private_view_key_encryption_key=wiring.monero_private_view_key_encryption_key,
            ),
            account_service=AccountService.on_session(session),
            account_event_service=AccountEventService.on_session(session),
        )

    async def find_last_received_subaddress_minor(self, monero_wallet_id: int) -> int:
        """What ``MoneroWalletService.allocate_invoice_subaddress_minor`` takes as ``last_received_minor``."""
        return await self.invoice_repository.find_last_received_subaddress_minor(monero_wallet_id)

    def list_monero_chain_summaries(self) -> list[MoneroChainSummary]:
        return [
            MoneroChainSummary(key=chain.key, name=chain.name, network=MoneroNetwork(chain.network))
            for chain in self.chains_service.monero_chains
        ]

    async def present_wallet_settings(self, wallet: MoneroWalletValue | None) -> MoneroWalletSettingsResponse:
        monero_wallet = None
        if wallet is not None:
            last_received_minor = await self.find_last_received_subaddress_minor(wallet.id)
            monero_wallet = MoneroWallet(
                chain_key=wallet.chain_key,
                primary_address=wallet.primary_address,
                network=wallet.network,
                confirmed_lookahead_minor_count=wallet.confirmed_lookahead_minor_count,
                unreceived_minor_gap=wallet.unreceived_minor_gap(last_received_minor),
                lookahead_warning=wallet.is_past_lookahead_warning(last_received_minor),
                created_at=wallet.created_at,
            )
        return MoneroWalletSettingsResponse(monero_chains=self.list_monero_chain_summaries(), wallet=monero_wallet)

    async def read_wallet_settings(self, account_id: int, *, chain_key: str | None) -> MoneroWalletSettingsResponse:
        """``chain_key`` left out: the first enabled Monero chain; with none enabled, no chains and no wallet."""
        if chain_key is None:
            monero_chains = self.chains_service.monero_chains
            if not monero_chains:
                return MoneroWalletSettingsResponse(monero_chains=[], wallet=None)
            chain_key = monero_chains[0].key
        self.chains_service.get_monero_chain(chain_key)
        return await self.present_wallet_settings(
            await self.monero_wallet_service.find_active_wallet(account_id, chain_key=chain_key)
        )

    async def create_change_options(
        self, account_access: AccountAccess, *, chain_key: str, primary_address: str
    ) -> dict[str, JsonValue]:
        """Passkey check options approving this one wallet. No Monero chain enabled here: 400 ``chain_not_enabled``,
        and no challenge is stored."""
        if not self.chains_service.monero_chains:
            raise ChainNotEnabledError("no Monero chain is enabled here")
        self.chains_service.get_monero_chain(chain_key)
        return await self.account_service.create_fresh_passkey_check_options(
            account_access,
            purpose=ChallengePurpose.CHANGE_MONERO_WALLET,
            requested_change_digest=hash_monero_wallet_change(chain_key, primary_address=primary_address),
        )

    async def set_wallet(
        self,
        account_access: AccountAccess,
        *,
        chain_key: str,
        primary_address: str,
        private_view_key: SecretStr,
        credential_json: dict[str, JsonValue],
    ) -> MoneroWalletSettingsResponse:
        """Sets the account's wallet of ``chain_key`` once the sidecar accepts the address and view key and the
        passkey check made for this wallet verifies, and records ``account.monero_wallet_set``. Invoices already made
        keep their wallet."""
        await self.monero_wallet_service.check_wallet_change(
            account_access.account_id,
            chain_key=chain_key,
            primary_address=primary_address,
            private_view_key=private_view_key,
        )
        try:
            await self.account_service.verify_fresh_passkey_check(
                account_access,
                purpose=ChallengePurpose.CHANGE_MONERO_WALLET,
                credential_json=credential_json,
                requested_change_digest=hash_monero_wallet_change(chain_key, primary_address=primary_address),
            )
        except AppError as refusal:
            log.emit(
                logger,
                event=account_log_events.PasskeyCeremonyRefused(ceremony="change_monero_wallet", reason=refusal.code),
            )
            raise
        wallet = await self.monero_wallet_service.save_checked_wallet(
            account_access.account_id,
            chain_key=chain_key,
            primary_address=primary_address,
            private_view_key=private_view_key,
        )
        await self.account_event_service.record_account_event(
            None,
            account_id=account_access.account_id,
            event_type=AccountEventType.MONERO_WALLET_SET,
            data={"chain_key": chain_key},
        )
        return await self.present_wallet_settings(wallet)

    async def confirm_lookahead(self, account_access: AccountAccess, *, chain_key: str) -> MoneroWalletSettingsResponse:
        return await self.present_wallet_settings(
            await self.monero_wallet_service.confirm_lookahead(account_access.account_id, chain_key=chain_key)
        )
