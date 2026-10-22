"""MoneroWalletService: an account's Monero wallet and the subaddress minors its XMR invoices take (PLAN.md section 14).

- ``check_wallet_change`` runs the address and view key checks (the sidecar's ``POST /v1/wallet-checks``; no sidecar
  set up means 503 ``monero_sidecar_unavailable``) on the chain's own network and the unchanged and taken refusals;
  the caller then has the fresh passkey check, as for the payout address, and ``save_checked_wallet`` saves it. A
  change inserts a new row and marks the old one replaced; invoices keep the wallet they were made with. Going back to
  an address the account held before reactivates its old row with its counters, and an address another account holds
  is refused: one row per address and chain, so no minor is ever derived twice.
- ``allocate_invoice_subaddress_minor`` takes the next minor under the wallet row's lock, so two invoices never share a
  subaddress, and refuses at the lookahead limit: a receiver's wallet scans its lookahead past its highest minor with
  a received output, so a longer run of unreceived minors could blind it to the next payment. Refusing beats burning.
- ``confirm_lookahead`` records that the receiver ran ``set subaddress-lookahead 50:1000``: the gap guard then trusts
  1000 minors. Session only, no passkey: it can only make the account's own wallet refuse fewer invoices.
"""

from datetime import UTC, datetime
from typing import NoReturn, Self

from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.monero_sidecar_api.errors import MoneroSidecarError, MoneroSidecarInputRefusedError
from app.payments.chains.service import ChainsService
from app.payments.monero_wallets import log_events
from app.payments.monero_wallets.enums import MoneroNetwork
from app.payments.monero_wallets.errors import (
    MoneroAddressInvalidError,
    MoneroAddressTakenError,
    MoneroLookaheadExhaustedError,
    MoneroSidecarUnavailableError,
    MoneroViewKeyMismatchError,
    MoneroWalletChangeConflictError,
    MoneroWalletMissingError,
    MoneroWalletUnchangedError,
)
from app.payments.monero_wallets.models import PRIMARY_ADDRESS_LENGTH, AccountMoneroWallet
from app.payments.monero_wallets.repository import MoneroWalletRepository
from app.payments.monero_wallets.secret_sealing import open_private_view_key, seal_private_view_key
from app.payments.monero_wallets.types import (
    MoneroSubaddressAllocation,
    MoneroWalletSidecar,
    MoneroWalletValue,
)
from app.shared import logging as log

logger = log.get_logger(__name__)

# A subaddress is 95 characters, and its first character names its network: network byte 42 on mainnet ("8"), 36 on
# stagenet ("7") (https://docs.getmonero.org/public-address/subaddress/). A primary address ("4", "5") back from the
# sidecar would be a derivation gone wrong, and money paid to it would never reach the invoice.
SUBADDRESS_LENGTH = 95
SUBADDRESS_FIRST_CHARACTER_BY_NETWORK = {MoneroNetwork.MAINNET: "8", MoneroNetwork.STAGENET: "7"}


def present_wallet_value(wallet: AccountMoneroWallet) -> MoneroWalletValue:
    return present_wallet_value_with_confirmed_lookahead(
        wallet, confirmed_lookahead_minor_count=wallet.confirmed_lookahead_minor_count
    )


def present_wallet_value_with_confirmed_lookahead(
    wallet: AccountMoneroWallet, *, confirmed_lookahead_minor_count: int
) -> MoneroWalletValue:
    """The wallet with a lookahead just confirmed in this transaction, which the loaded row does not show yet."""
    return MoneroWalletValue(
        id=wallet.id,
        account_id=wallet.account_id,
        chain_key=wallet.chain_key,
        primary_address=wallet.primary_address,
        network=wallet.network,
        major_index=wallet.major_index,
        next_minor_index=wallet.next_minor_index,
        confirmed_lookahead_minor_count=confirmed_lookahead_minor_count,
        created_at=wallet.created_at,
    )


async def list_active_monero_wallet_chain_keys(session: AsyncSession, *, account_id: int) -> frozenset[str]:
    """The Monero chains on which the account has an active wallet: the ones its accepted coins list XMR on
    (``AccountPreferencesService.resolve_accepted_asset_symbols_by_chain``). A read needs no sidecar, so it is not a
    service method."""
    return frozenset(await MoneroWalletRepository(session=session).list_active_wallet_chain_keys(account_id))


class MoneroWalletService:
    def __init__(
        self,
        *,
        repository: MoneroWalletRepository,
        chains_service: ChainsService,
        monero_sidecar: MoneroWalletSidecar,
        private_view_key_encryption_key: bytes,
    ) -> None:
        self.repository = repository
        self.chains_service = chains_service
        # The sidecar client for the key checks and the derivation; ``UnavailableMoneroSidecar`` when none is set up.
        self.monero_sidecar = monero_sidecar
        self.private_view_key_encryption_key = private_view_key_encryption_key

    @classmethod
    def on_session(
        cls,
        session: AsyncSession,
        *,
        chains_service: ChainsService,
        monero_sidecar: MoneroWalletSidecar,
        private_view_key_encryption_key: bytes,
    ) -> Self:
        return cls(
            repository=MoneroWalletRepository(session=session),
            chains_service=chains_service,
            monero_sidecar=monero_sidecar,
            private_view_key_encryption_key=private_view_key_encryption_key,
        )

    async def find_active_wallet(self, account_id: int, *, chain_key: str) -> MoneroWalletValue | None:
        wallet = await self.repository.find_active_wallet(account_id, chain_key=chain_key)
        return present_wallet_value(wallet) if wallet is not None else None

    async def check_wallet_change(
        self, account_id: int, *, chain_key: str, primary_address: str, private_view_key: SecretStr
    ) -> None:
        """Every refusal a wallet change can meet without a race, run before the passkey check consumes its challenge:
        ``chain_key`` an enabled Monero chain (400 ``chain_not_enabled`` for a chain not enabled here,
        ``chain_kind_not_supported`` for an enabled chain of another kind), the address a valid primary
        address of that chain's network, the view key its own (the sidecar's checks), not the active wallet already,
        and not held by another account on that chain. Reads without locks; ``save_checked_wallet`` re-checks the
        last two under its lock."""
        network = MoneroNetwork(self.chains_service.get_monero_chain(chain_key).network)
        await self.check_wallet_keys(primary_address, private_view_key=private_view_key, network=network)
        active_wallet = await self.repository.find_active_wallet(account_id, chain_key=chain_key)
        if active_wallet is not None and active_wallet.primary_address == primary_address:
            raise MoneroWalletUnchangedError("primary_address: already the account's Monero wallet")
        earlier_wallet_of_address = await self.repository.find_wallet_by_address(primary_address, chain_key=chain_key)
        if earlier_wallet_of_address is not None and earlier_wallet_of_address.account_id != account_id:
            raise MoneroAddressTakenError("primary_address: another account uses this Monero wallet")

    async def save_checked_wallet(
        self, account_id: int, *, chain_key: str, primary_address: str, private_view_key: SecretStr
    ) -> MoneroWalletValue:
        """Saves a wallet ``check_wallet_change`` passed (no sidecar call here): only a change that landed in between
        refuses now (``monero_wallet_unchanged``, ``monero_address_taken``, ``monero_wallet_change_conflict``)."""
        network = MoneroNetwork(self.chains_service.get_monero_chain(chain_key).network)
        active_wallet = await self.repository.find_active_wallet(account_id, chain_key=chain_key, lock=True)
        if active_wallet is not None and active_wallet.primary_address == primary_address:
            raise MoneroWalletUnchangedError("primary_address: already the account's Monero wallet")
        # Read without a lock: another account's row is only refused, never written, and locking it could deadlock two
        # accounts swapping addresses. The account's own rows are serialized by the active row's lock above.
        earlier_wallet_of_address = await self.repository.find_wallet_by_address(primary_address, chain_key=chain_key)
        if earlier_wallet_of_address is not None and earlier_wallet_of_address.account_id != account_id:
            raise MoneroAddressTakenError("primary_address: another account uses this Monero wallet")
        # One savepoint around the replace and the insert or reactivation: a unique violation (two first wallets of one
        # account, or one address set by two accounts at once, since the lock above holds only rows that exist) rolls
        # the replace back too, so a caller that keeps the transaction never sees an account without an active wallet.
        try:
            async with self.repository.session.begin_nested():
                if active_wallet is not None:
                    await self.repository.mark_replaced(active_wallet.id, replaced_at=datetime.now(UTC))
                if earlier_wallet_of_address is not None:
                    await self.repository.reactivate_wallet(earlier_wallet_of_address.id)
                    wallet = earlier_wallet_of_address
                else:
                    wallet = await self.repository.insert_wallet(
                        account_id=account_id,
                        chain_key=chain_key,
                        primary_address=primary_address,
                        sealed_private_view_key=seal_private_view_key(
                            private_view_key,
                            account_id=account_id,
                            chain_key=chain_key,
                            primary_address=primary_address,
                            encryption_key=self.private_view_key_encryption_key,
                        ),
                        network=network,
                    )
        except IntegrityError as unique_violation:
            await self.raise_concurrent_wallet_change(
                account_id, chain_key=chain_key, primary_address=primary_address, unique_violation=unique_violation
            )
        log.emit(
            logger,
            event=log_events.MoneroWalletSet(
                account_id=account_id,
                chain_key=chain_key,
                wallet_id=wallet.id,
                replaced_wallet_id=active_wallet.id if active_wallet is not None else None,
                reactivated=earlier_wallet_of_address is not None,
            ),
        )
        return present_wallet_value(wallet)

    async def check_wallet_keys(
        self, primary_address: str, *, private_view_key: SecretStr, network: MoneroNetwork
    ) -> None:
        """The sidecar's checks; refuses an invalid address, a view key of another address, or no answer."""
        if len(primary_address) != PRIMARY_ADDRESS_LENGTH:
            # An integrated address or a subaddress is never a wallet's primary address; the column holds 95.
            raise MoneroAddressInvalidError("primary_address: not a valid Monero primary address of this network")
        try:
            wallet_check = await self.monero_sidecar.check_wallet(
                primary_address, private_view_key=private_view_key, network=network
            )
        except MoneroSidecarInputRefusedError as input_refusal:
            # The view key's format is checked at the edge (64 hex), so what the checker cannot parse is the address.
            raise MoneroAddressInvalidError(
                "primary_address: not a valid Monero primary address of this network"
            ) from input_refusal
        except MoneroSidecarError as sidecar_failure:
            raise MoneroSidecarUnavailableError(
                "monero_wallet: Monero wallet checks are not available right now"
            ) from sidecar_failure
        if not wallet_check.address_valid:
            raise MoneroAddressInvalidError("primary_address: not a valid Monero primary address of this network")
        if not wallet_check.view_key_matches:
            raise MoneroViewKeyMismatchError("private_view_key: does not belong to this address")

    async def confirm_lookahead(self, account_id: int, *, chain_key: str) -> MoneroWalletValue:
        """The receiver widened its wallet's lookahead to ``widened_lookahead_minor_count`` minors; the active wallet
        trusts that many from now on. Repeating it changes nothing."""
        self.chains_service.get_monero_chain(chain_key)
        wallet = await self.repository.find_active_wallet(account_id, chain_key=chain_key, lock=True)
        if wallet is None:
            raise MoneroWalletMissingError("monero_wallet: set a Monero wallet in settings first")
        confirmed_lookahead_minor_count = MoneroWalletValue.widened_lookahead_minor_count
        if wallet.confirmed_lookahead_minor_count != confirmed_lookahead_minor_count:
            await self.repository.confirm_lookahead(
                wallet.id, confirmed_lookahead_minor_count=confirmed_lookahead_minor_count
            )
            log.emit(
                logger,
                event=log_events.MoneroLookaheadConfirmed(
                    wallet_id=wallet.id, confirmed_lookahead_minor_count=confirmed_lookahead_minor_count
                ),
            )
        return present_wallet_value_with_confirmed_lookahead(
            wallet, confirmed_lookahead_minor_count=confirmed_lookahead_minor_count
        )

    async def raise_concurrent_wallet_change(
        self, account_id: int, *, chain_key: str, primary_address: str, unique_violation: IntegrityError
    ) -> NoReturn:
        """Turns a unique violation of a concurrent wallet change into the 409 it amounts to, read back after the
        savepoint rolled back: the same wallet already active, the address taken by another account, or a plain
        conflict."""
        concurrent_active_wallet = await self.repository.find_active_wallet(account_id, chain_key=chain_key)
        if concurrent_active_wallet is not None and concurrent_active_wallet.primary_address == primary_address:
            raise MoneroWalletUnchangedError(
                "primary_address: already the account's Monero wallet"
            ) from unique_violation
        concurrent_wallet_of_address = await self.repository.find_wallet_by_address(
            primary_address, chain_key=chain_key
        )
        if concurrent_wallet_of_address is not None and concurrent_wallet_of_address.account_id != account_id:
            raise MoneroAddressTakenError(
                "primary_address: another account uses this Monero wallet"
            ) from unique_violation
        raise MoneroWalletChangeConflictError(
            "monero_wallet: another change of this Monero wallet landed at the same time; reload and try again"
        ) from unique_violation

    async def allocate_invoice_subaddress_minor(
        self, account_id: int, *, chain_key: str, last_received_minor: int
    ) -> MoneroSubaddressAllocation:
        """``last_received_minor``: the highest minor of this wallet with an output seen in a block (0 when none),
        which the invoices domain knows from ``monero_payments``. That is a received output, not an invoice in status
        ``paid``: an underpaid invoice's output moves the receiver's wallet lookahead too. Raises
        ``MoneroLookaheadExhaustedError`` at the refusal gap."""
        wallet = await self.repository.find_active_wallet(account_id, chain_key=chain_key, lock=True)
        if wallet is None:
            # A `FOR UPDATE` that waited on a wallet change sees the old row replaced and not the new one (that
            # statement's snapshot predates the commit); one more read sees it.
            wallet = await self.repository.find_active_wallet(account_id, chain_key=chain_key, lock=True)
        if wallet is None:
            raise MoneroWalletMissingError("monero_wallet: set a Monero wallet in settings first")
        wallet_value = present_wallet_value(wallet)
        unreceived_minor_gap = wallet_value.unreceived_minor_gap(last_received_minor)
        if wallet_value.is_past_lookahead_refusal(last_received_minor):
            log.emit(
                logger,
                event=log_events.MoneroLookaheadRefused(
                    wallet_id=wallet.id,
                    unreceived_minor_gap=unreceived_minor_gap,
                    confirmed_lookahead_minor_count=wallet_value.confirmed_lookahead_minor_count,
                ),
            )
            raise MoneroLookaheadExhaustedError(
                "monero_wallet: too many unpaid subaddresses; widen the wallet lookahead"
            )
        subaddress_minor = await self.repository.take_next_minor(wallet.id)
        log.emit(
            logger, event=log_events.MoneroSubaddressAllocated(wallet_id=wallet.id, subaddress_minor=subaddress_minor)
        )
        lookahead_warning = wallet_value.is_past_lookahead_warning(last_received_minor)
        if lookahead_warning:
            log.emit(
                logger,
                event=log_events.MoneroLookaheadWarning(
                    wallet_id=wallet.id,
                    unreceived_minor_gap=unreceived_minor_gap,
                    confirmed_lookahead_minor_count=wallet_value.confirmed_lookahead_minor_count,
                ),
            )
        return MoneroSubaddressAllocation(
            monero_wallet_id=wallet.id,
            primary_address=wallet.primary_address,
            subaddress_minor=subaddress_minor,
            lookahead_warning=lookahead_warning,
        )

    async def derive_invoice_subaddress(self, allocation: MoneroSubaddressAllocation) -> str:
        """The subaddress of the allocated minor, from the sidecar. The sealed view key is opened here for this one
        call and never returned or logged. Refused as unavailable without a sidecar, when it does not answer, or when
        it answers something that is not a subaddress of the wallet's network."""
        wallet = await self.repository.find_wallet(allocation.monero_wallet_id)
        if wallet is None:
            raise LookupError(f"Monero wallet {allocation.monero_wallet_id} does not exist")
        try:
            subaddress = await self.monero_sidecar.derive_subaddress(
                wallet.primary_address,
                private_view_key=open_private_view_key(
                    wallet.sealed_private_view_key,
                    account_id=wallet.account_id,
                    chain_key=wallet.chain_key,
                    primary_address=wallet.primary_address,
                    encryption_key=self.private_view_key_encryption_key,
                ),
                network=wallet.network,
                major_index=wallet.major_index,
                minor_index=allocation.subaddress_minor,
            )
        except (MoneroSidecarError, MoneroSidecarInputRefusedError) as sidecar_failure:
            # A 400 here means a saved wallet the sidecar no longer accepts: unavailable, never an unchecked address.
            raise MoneroSidecarUnavailableError(
                "the Monero scanner is not available right now; try again later"
            ) from sidecar_failure
        if len(subaddress) != SUBADDRESS_LENGTH or not subaddress.startswith(
            SUBADDRESS_FIRST_CHARACTER_BY_NETWORK[wallet.network]
        ):
            log.emit(logger, event=log_events.MoneroSubaddressRefused(wallet_id=wallet.id))
            raise MoneroSidecarUnavailableError("the Monero scanner answered something that is not a subaddress")
        return subaddress
