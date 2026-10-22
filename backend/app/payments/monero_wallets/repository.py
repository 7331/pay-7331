"""Queries for account_monero_wallets; no decisions."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.monero_wallets.enums import MoneroNetwork
from app.payments.monero_wallets.models import AccountMoneroWallet


class MoneroWalletRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def find_active_wallet(
        self, account_id: int, *, chain_key: str, lock: bool = False
    ) -> AccountMoneroWallet | None:
        """``lock`` (``FOR UPDATE``) serializes allocations of minors and wallet changes of one account and chain."""
        statement = select(AccountMoneroWallet).where(
            AccountMoneroWallet.account_id == account_id,
            AccountMoneroWallet.chain_key == chain_key,
            AccountMoneroWallet.replaced_at.is_(None),
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self.session.scalar(statement)

    async def list_active_wallet_chain_keys(self, account_id: int) -> list[str]:
        """The chains on which the account has an active wallet (``ux_account_monero_wallets_active``)."""
        return list(
            await self.session.scalars(
                select(AccountMoneroWallet.chain_key).where(
                    AccountMoneroWallet.account_id == account_id, AccountMoneroWallet.replaced_at.is_(None)
                )
            )
        )

    async def find_wallet(self, wallet_id: int) -> AccountMoneroWallet | None:
        return await self.session.get(AccountMoneroWallet, wallet_id)

    async def find_wallet_by_address(self, primary_address: str, *, chain_key: str) -> AccountMoneroWallet | None:
        """The one row of this address on this chain, active or replaced, of any account
        (``ux_account_monero_wallets_chain_key_primary_address``). Never locked: another account's row is only read."""
        return await self.session.scalar(
            select(AccountMoneroWallet).where(
                AccountMoneroWallet.chain_key == chain_key, AccountMoneroWallet.primary_address == primary_address
            )
        )

    async def insert_wallet(
        self,
        *,
        account_id: int,
        chain_key: str,
        primary_address: str,
        sealed_private_view_key: bytes,
        network: MoneroNetwork,
    ) -> AccountMoneroWallet:
        wallet = AccountMoneroWallet(
            account_id=account_id,
            chain_key=chain_key,
            primary_address=primary_address,
            sealed_private_view_key=sealed_private_view_key,
            network=network,
        )
        # Raises IntegrityError on either unique index (a concurrent first wallet, or the same address on another
        # account); the service wraps it in the savepoint that keeps the transaction usable for its re-read.
        self.session.add(wallet)
        await self.session.flush()
        await self.session.refresh(wallet)
        return wallet

    async def mark_replaced(self, wallet_id: int, *, replaced_at: datetime) -> None:
        await self.session.execute(
            update(AccountMoneroWallet).where(AccountMoneroWallet.id == wallet_id).values(replaced_at=replaced_at)
        )

    async def reactivate_wallet(self, wallet_id: int) -> None:
        """Clears ``replaced_at`` and keeps every counter. The caller has marked the active wallet replaced first, in
        its savepoint: raises IntegrityError on ``ux_account_monero_wallets_active`` when a concurrent first wallet of
        the account landed."""
        await self.session.execute(
            update(AccountMoneroWallet).where(AccountMoneroWallet.id == wallet_id).values(replaced_at=None)
        )

    async def take_next_minor(self, wallet_id: int) -> int:
        """Returns the minor taken and moves ``next_minor_index`` past it. The caller holds the wallet row's lock."""
        taken_minor = await self.session.scalar(
            update(AccountMoneroWallet)
            .where(AccountMoneroWallet.id == wallet_id)
            .values(next_minor_index=AccountMoneroWallet.next_minor_index + 1)
            .returning(AccountMoneroWallet.next_minor_index - 1)
        )
        if taken_minor is None:
            raise LookupError(f"Monero wallet {wallet_id} does not exist")
        return taken_minor

    async def confirm_lookahead(self, wallet_id: int, *, confirmed_lookahead_minor_count: int) -> None:
        """The receiver widened the wallet's lookahead to ``confirmed_lookahead_minor_count`` minors."""
        await self.session.execute(
            update(AccountMoneroWallet)
            .where(AccountMoneroWallet.id == wallet_id)
            .values(confirmed_lookahead_minor_count=confirmed_lookahead_minor_count)
        )
