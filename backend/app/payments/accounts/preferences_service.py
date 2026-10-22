"""AccountPreferencesService: what an account saves for its invoices, read back for sessions, API tokens, invoice
creation and payer pages (PLAN.md section 7).

- Accepted coins: each enabled chain's starting ticks, the saved ones the chain still lists, else
  its defaults; XMR only on a chain where the account has an active Monero wallet.
- Confirmations before final: a count per enabled Monero chain, else the chain's default.
- Payment window and accepted shortfall: per enabled chain, what the account's invoices take when their maker sets
  none, else the server's default (``chains/invoice_payment_terms.py``).
- The support contact is an email address, a Telegram handle or an https link; payer pages link it.
"""

from decimal import Decimal
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.accounts import log_events
from app.payments.accounts.repository import AccountRepository
from app.payments.accounts.support_contacts import normalize_support_contact, parse_support_contact
from app.payments.accounts.types import AccountAccess, SavedInvoicePaymentTerms, SellerSupportContact
from app.payments.chains.configuration import read_enabled_chains
from app.payments.chains.invoice_payment_terms import format_accuracy_percent
from app.payments.chains.service import ChainsService
from app.payments.chains.types import MoneroChainConfiguration
from app.payments.monero_wallets.service import list_active_monero_wallet_chain_keys
from app.shared import logging as log
from app.shared.errors import InvalidInputError

logger = log.get_logger(__name__)


class AccountPreferencesService:
    def __init__(self, *, repository: AccountRepository, chains_service: ChainsService) -> None:
        self.repository = repository
        self.chains_service = chains_service

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(
            repository=AccountRepository(session=session),
            chains_service=ChainsService(chains=read_enabled_chains()),
        )

    # ---- Accepted coins ----

    async def resolve_accepted_asset_symbols_by_chain(
        self, account_id: int, *, saved_by_chain: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        """Every enabled chain's starting coins: the saved ones the chain still lists, in its order, else its
        defaults. A saved chain that is not enabled here is left out (kept in the row), and so is a Monero chain the
        account has no active wallet on: XMR invoices need one (PLAN.md section 14), so XMR is never offered without
        it. The wallet read runs only while a Monero chain is enabled."""
        monero_wallet_chain_keys: frozenset[str] = frozenset()
        if self.chains_service.monero_chains:
            monero_wallet_chain_keys = await list_active_monero_wallet_chain_keys(
                self.repository.session, account_id=account_id
            )
        resolved_by_chain: dict[str, list[str]] = {}
        for chain in self.chains_service.chains:
            if isinstance(chain, MoneroChainConfiguration) and chain.key not in monero_wallet_chain_keys:
                continue
            saved_symbols = chain.order_accepted_asset_symbols(saved_by_chain.get(chain.key, []))
            resolved_by_chain[chain.key] = saved_symbols or chain.default_accepted_asset_symbols
        return resolved_by_chain

    async def save_accepted_asset_symbols(
        self, account_access: AccountAccess, *, accepted_asset_symbols_by_chain: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        """Saves the named chains' coins as the account's starting ticks (an unknown chain or symbol is 400, an empty
        list too); answers every chain's starting coins after the save."""
        normalized_by_chain: dict[str, list[str]] = {}
        for chain_key, asset_symbols in accepted_asset_symbols_by_chain.items():
            chain = self.chains_service.get_chain(chain_key)
            if (
                isinstance(chain, MoneroChainConfiguration)
                and chain.key not in account_access.accepted_asset_symbols_by_chain
            ):
                raise InvalidInputError(f"accepted_assets_by_chain.{chain_key}: set a Monero wallet in settings first")
            for asset_symbol in asset_symbols:
                self.chains_service.get_asset(chain.key, symbol=asset_symbol)
            normalized_symbols = chain.order_accepted_asset_symbols(asset_symbols)
            if not normalized_symbols:
                raise InvalidInputError(f"accepted_assets_by_chain.{chain_key}: tick at least one coin")
            normalized_by_chain[chain.key] = normalized_symbols
        if normalized_by_chain:
            await self.repository.merge_accepted_asset_symbols(
                account_access.account_id, accepted_asset_symbols_by_chain=normalized_by_chain
            )
            log.emit(
                logger,
                event=log_events.AccountAcceptedAssetsSaved(
                    account_id=account_access.account_id, chain_keys=sorted(normalized_by_chain)
                ),
            )
        return {**account_access.accepted_asset_symbols_by_chain, **normalized_by_chain}

    # ---- Confirmations before final ----

    def resolve_confirmations_final_by_chain(self, *, saved_by_chain: dict[str, int]) -> dict[str, int]:
        """The enabled Monero chains' counts: the saved one where the account set it, nothing where it did not (the
        payer pages and the settings sheet then stand at the chain's ``chains.toml`` default). A saved key of a chain
        that is not enabled here is left out (kept in the row)."""
        return {
            chain.key: saved_by_chain[chain.key]
            for chain in self.chains_service.chains
            if isinstance(chain, MoneroChainConfiguration) and chain.key in saved_by_chain
        }

    async def save_confirmations_final_by_chain(
        self, account_access: AccountAccess, *, confirmations_final_by_chain: dict[str, int | None]
    ) -> dict[str, int]:
        """Saves the named Monero chains' counts (a chain of another kind, an unknown one or a value outside 1 to 100
        is 400); a `null` clears the chain's saved count back to the chain default. Answers every enabled Monero
        chain's count after the save."""
        normalized_by_chain: dict[str, int | None] = {}
        for chain_key, confirmations_final in confirmations_final_by_chain.items():
            chain = self.chains_service.get_chain(chain_key)
            if not isinstance(chain, MoneroChainConfiguration):
                raise InvalidInputError(f"confirmations_final_by_chain.{chain_key}: only a Monero chain takes a count")
            normalized_by_chain[chain.key] = confirmations_final
        if normalized_by_chain:
            await self.repository.merge_confirmations_final_by_chain(
                account_access.account_id, confirmations_final_by_chain=normalized_by_chain
            )
            log.emit(
                logger,
                event=log_events.AccountConfirmationsFinalSaved(
                    account_id=account_access.account_id, chain_keys=sorted(normalized_by_chain)
                ),
            )
        saved_by_chain = await self.repository.find_confirmations_final_by_chain(account_access.account_id)
        return self.resolve_confirmations_final_by_chain(saved_by_chain=saved_by_chain)

    async def find_saved_confirmations_final_by_chain(self, account_id: int) -> dict[str, int]:
        """The account's saved counts as saved, for an invoice's creation: the caller decides the chain default."""
        return await self.repository.find_confirmations_final_by_chain(account_id)

    # ---- Payment window and accepted shortfall ----

    def resolve_saved_values_of_enabled_chains[SavedValue](
        self, saved_by_chain: dict[str, SavedValue]
    ) -> dict[str, SavedValue]:
        """The saved values of the chains enabled here, in their order; a chain left out (or saved but not enabled
        here, kept in the row) stands at the server's default."""
        return {
            chain.key: saved_by_chain[chain.key] for chain in self.chains_service.chains if chain.key in saved_by_chain
        }

    async def save_invoice_payment_terms_by_chain(
        self,
        account_access: AccountAccess,
        *,
        invoice_lifetime_seconds_by_chain: dict[str, int | None],
        accuracy_percent_by_chain: dict[str, Decimal | None],
    ) -> tuple[dict[str, int], dict[str, str]]:
        """Saves the named chains' payment windows and shortfalls (an unknown or disabled chain is 400, as the bounds
        are checked by the request); a `null` clears the chain's back to the server's default. Answers every enabled
        chain's saved ones after the save."""
        for chain_key in [*invoice_lifetime_seconds_by_chain, *accuracy_percent_by_chain]:
            self.chains_service.get_chain(chain_key)
        normalized_accuracy_percent_by_chain = {
            chain_key: format_accuracy_percent(accuracy_percent) if accuracy_percent is not None else None
            for chain_key, accuracy_percent in accuracy_percent_by_chain.items()
        }
        if invoice_lifetime_seconds_by_chain or normalized_accuracy_percent_by_chain:
            await self.repository.merge_invoice_payment_terms_by_chain(
                account_access.account_id,
                invoice_lifetime_seconds_by_chain=invoice_lifetime_seconds_by_chain,
                accuracy_percent_by_chain=normalized_accuracy_percent_by_chain,
            )
            log.emit(
                logger,
                event=log_events.AccountInvoicePaymentTermsSaved(
                    account_id=account_access.account_id,
                    chain_keys=sorted({*invoice_lifetime_seconds_by_chain, *normalized_accuracy_percent_by_chain}),
                ),
            )
        (
            saved_lifetime_seconds_by_chain,
            saved_accuracy_percent_by_chain,
        ) = await self.repository.find_invoice_payment_terms_by_chain(account_access.account_id)
        return (
            self.resolve_saved_values_of_enabled_chains(saved_lifetime_seconds_by_chain),
            self.resolve_saved_values_of_enabled_chains(saved_accuracy_percent_by_chain),
        )

    async def find_saved_invoice_payment_terms_by_chain(self, account_id: int) -> SavedInvoicePaymentTerms:
        """The account's saved payment windows and shortfalls as saved, for an invoice's creation: the caller decides
        the server default."""
        (
            saved_lifetime_seconds_by_chain,
            saved_accuracy_percent_by_chain,
        ) = await self.repository.find_invoice_payment_terms_by_chain(account_id)
        return SavedInvoicePaymentTerms(
            lifetime_seconds_by_chain=saved_lifetime_seconds_by_chain,
            accuracy_percent_by_chain={
                chain_key: Decimal(accuracy_percent_text)
                for chain_key, accuracy_percent_text in saved_accuracy_percent_by_chain.items()
            },
        )

    # ---- Support contact ----

    async def save_support_contact(self, account_access: AccountAccess, *, support_contact: str | None) -> str | None:
        """Saves the contact (empty clears it); answers what is saved now."""
        normalized_support_contact = normalize_support_contact(support_contact)
        await self.repository.update_support_contact(
            account_access.account_id, support_contact=normalized_support_contact
        )
        log.emit(
            logger,
            event=log_events.AccountSupportContactSaved(
                account_id=account_access.account_id, support_contact_set=normalized_support_contact is not None
            ),
        )
        return normalized_support_contact

    async def find_seller_support_contact(self, account_id: int | None) -> SellerSupportContact | None:
        """The contact an invoice's payer sees: its account's, linked; None for a signed-out invoice or none set."""
        if account_id is None:
            return None
        support_contact = await self.repository.find_support_contact(account_id)
        return parse_support_contact(support_contact) if support_contact else None
