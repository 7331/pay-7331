"""Queries for static_deposit_addresses and static_deposit_payments; no decisions."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.static_deposit_addresses.models import StaticDepositAddress, StaticDepositPayment
from app.payments.static_deposit_addresses.types import StaticDepositAddressTotals


def scan_tier_condition(*, hot: bool, hot_since: datetime) -> ColumnElement[bool]:
    """The scan's hot tier: made or paid since ``hot_since``; the cold tier is every other address."""
    is_hot = or_(StaticDepositAddress.created_at >= hot_since, StaticDepositAddress.last_received_at >= hot_since)
    if hot:
        return is_hot
    return and_(
        StaticDepositAddress.created_at < hot_since,
        or_(StaticDepositAddress.last_received_at.is_(None), StaticDepositAddress.last_received_at < hot_since),
    )


# pg_advisory_xact_lock's key space is shared by every caller: this one hashes its own prefix and the account id.
ADVISORY_LOCK_PREFIX = "static_deposit_address_creation:"


class StaticDepositAddressRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def lock_account_creations(self, account_id: int) -> None:
        """Serialises one account's creations until the transaction ends, so a repeated customer reference makes one
        row and the per-account cap holds."""
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"{ADVISORY_LOCK_PREFIX}{account_id}", 0)))
        )

    async def insert_static_deposit_address(self, static_deposit_address: StaticDepositAddress) -> None:
        self.session.add(static_deposit_address)
        await self.session.flush()

    async def find_by_customer_reference(
        self, account_id: int, *, chain_key: str, customer_reference: str
    ) -> StaticDepositAddress | None:
        return await self.session.scalar(
            select(StaticDepositAddress).where(
                StaticDepositAddress.account_id == account_id,
                StaticDepositAddress.chain_key == chain_key,
                StaticDepositAddress.customer_reference == customer_reference,
            )
        )

    async def count_account_static_deposit_addresses(self, account_id: int) -> int:
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(StaticDepositAddress)
                .where(StaticDepositAddress.account_id == account_id)
            )
            or 0
        )

    async def exists_deposit_address(self, address: str) -> bool:
        return (
            await self.session.scalar(
                select(StaticDepositAddress.id).where(StaticDepositAddress.deposit_address == address).limit(1)
            )
            is not None
        )

    async def get_static_deposit_address(
        self, static_deposit_address_id: str, *, lock: bool = False
    ) -> StaticDepositAddress | None:
        statement = select(StaticDepositAddress).where(StaticDepositAddress.id == static_deposit_address_id)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def list_static_deposit_addresses(self, static_deposit_address_ids: list[str]) -> list[StaticDepositAddress]:
        if not static_deposit_address_ids:
            return []
        return list(
            await self.session.scalars(
                select(StaticDepositAddress).where(StaticDepositAddress.id.in_(static_deposit_address_ids))
            )
        )

    async def lock_static_deposit_addresses(self, static_deposit_address_ids: list[str]) -> list[StaticDepositAddress]:
        """Locked ``FOR UPDATE`` in id order, so two lockers never deadlock."""
        if not static_deposit_address_ids:
            return []
        return list(
            await self.session.scalars(
                select(StaticDepositAddress)
                .where(StaticDepositAddress.id.in_(static_deposit_address_ids))
                .order_by(StaticDepositAddress.id)
                .with_for_update()
            )
        )

    async def list_account_static_deposit_addresses(
        self,
        account_id: int,
        *,
        chain_key: str | None,
        customer_reference: str | None,
        customer_reference_prefix: str | None,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> list[StaticDepositAddress]:
        """The account's addresses, newest first; ``after`` is the (created_at, id) of the previous page's last row.
        The prefix is matched literally (``%`` and ``_`` escaped) over the pattern index."""
        statement = (
            select(StaticDepositAddress)
            .where(StaticDepositAddress.account_id == account_id)
            .order_by(StaticDepositAddress.created_at.desc(), StaticDepositAddress.id.desc())
            .limit(limit)
        )
        if chain_key is not None:
            statement = statement.where(StaticDepositAddress.chain_key == chain_key)
        if customer_reference is not None:
            statement = statement.where(StaticDepositAddress.customer_reference == customer_reference)
        if customer_reference_prefix is not None:
            statement = statement.where(
                StaticDepositAddress.customer_reference.startswith(customer_reference_prefix, autoescape=True)
            )
        if after is not None:
            statement = statement.where(
                tuple_(StaticDepositAddress.created_at, StaticDepositAddress.id) < tuple_(*after)
            )
        return list(await self.session.scalars(statement))

    async def list_chain_static_deposit_addresses_in_creation_order(
        self, chain_key: str, *, hot: bool, hot_since: datetime, after: tuple[datetime, str] | None, limit: int
    ) -> list[StaticDepositAddress]:
        """The watcher's scan of one tier: a chain's addresses oldest first, over
        ``ix_static_deposit_addresses_chain_key_created_at_id`` (the hot tier's paid ones also over
        ``ix_static_deposit_addresses_chain_key_last_received_at``)."""
        statement = (
            select(StaticDepositAddress)
            .where(
                StaticDepositAddress.chain_key == chain_key,
                scan_tier_condition(hot=hot, hot_since=hot_since),
            )
            .order_by(StaticDepositAddress.created_at, StaticDepositAddress.id)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(
                tuple_(StaticDepositAddress.created_at, StaticDepositAddress.id) > tuple_(*after)
            )
        return list(await self.session.scalars(statement))

    async def count_chain_static_deposit_addresses(self, chain_key: str, *, hot: bool, hot_since: datetime) -> int:
        """One tier's size, read once when its pass starts to pace it."""
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(StaticDepositAddress)
                .where(
                    StaticDepositAddress.chain_key == chain_key,
                    scan_tier_condition(hot=hot, hot_since=hot_since),
                )
            )
            or 0
        )

    async def list_sweep_blocked_static_deposit_addresses(self, chain_key: str) -> list[StaticDepositAddress]:
        """For the operator's alerts: the ones whose sweep failed, until ``sweep-static-deposit-address`` clears it."""
        return list(
            await self.session.scalars(
                select(StaticDepositAddress)
                .where(StaticDepositAddress.chain_key == chain_key, StaticDepositAddress.sweep_blocked.is_(True))
                .order_by(StaticDepositAddress.id)
            )
        )

    async def list_chain_static_deposit_addresses_after(
        self, chain_key: str, *, after_id: str | None, limit: int
    ) -> list[StaticDepositAddress]:
        """``reconcile``'s balance read: every address of the chain, a page at a time by id."""
        statement = (
            select(StaticDepositAddress)
            .where(StaticDepositAddress.chain_key == chain_key)
            .order_by(StaticDepositAddress.id)
            .limit(limit)
        )
        if after_id is not None:
            statement = statement.where(StaticDepositAddress.id > after_id)
        return list(await self.session.scalars(statement))

    async def sum_recorded_by_address_and_asset(
        self, static_deposit_address_ids: list[str]
    ) -> dict[tuple[str, str], int]:
        """What each address's payment rows hold per coin, credited or not: the part of its gross already
        explained."""
        if not static_deposit_address_ids:
            return {}
        rows = await self.session.execute(
            select(
                StaticDepositPayment.static_deposit_address_id,
                StaticDepositPayment.asset_symbol,
                func.sum(StaticDepositPayment.amount_base_units),
            )
            .where(StaticDepositPayment.static_deposit_address_id.in_(static_deposit_address_ids))
            .group_by(StaticDepositPayment.static_deposit_address_id, StaticDepositPayment.asset_symbol)
        )
        return {
            (static_deposit_address_id, asset_symbol): int(total)
            for static_deposit_address_id, asset_symbol, total in rows
        }

    async def list_recorded_transfer_keys(
        self, static_deposit_address_ids: list[str], *, transaction_hashes: list[str]
    ) -> set[tuple[str, str, int]]:
        """(address id, transaction hash, log index) of the attributed rows among the Transfer logs just read, so a
        Transfer read twice is one row."""
        if not static_deposit_address_ids or not transaction_hashes:
            return set()
        rows = await self.session.execute(
            select(
                StaticDepositPayment.static_deposit_address_id,
                StaticDepositPayment.transaction_hash,
                StaticDepositPayment.log_index,
            ).where(
                StaticDepositPayment.static_deposit_address_id.in_(static_deposit_address_ids),
                StaticDepositPayment.transaction_hash.in_(transaction_hashes),
            )
        )
        return {
            (static_deposit_address_id, transaction_hash, log_index)
            for static_deposit_address_id, transaction_hash, log_index in rows
            if transaction_hash is not None
        }

    async def insert_payment(self, payment: StaticDepositPayment) -> StaticDepositPayment:
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def list_payments(self, static_deposit_address_id: str, *, limit: int) -> list[StaticDepositPayment]:
        return list(
            await self.session.scalars(
                select(StaticDepositPayment)
                .where(StaticDepositPayment.static_deposit_address_id == static_deposit_address_id)
                .order_by(StaticDepositPayment.id.desc())
                .limit(limit)
            )
        )

    async def read_totals(self, static_deposit_address_ids: list[str]) -> dict[str, StaticDepositAddressTotals]:
        """Per address: its credited USD sum, how many payments it has (credited or not) and the newest one's time."""
        if not static_deposit_address_ids:
            return {}
        rows = await self.session.execute(
            select(
                StaticDepositPayment.static_deposit_address_id,
                func.coalesce(
                    func.sum(StaticDepositPayment.amount_usd).filter(StaticDepositPayment.credited.is_(True)), 0
                ),
                func.count(),
                func.max(StaticDepositPayment.created_at),
            )
            .where(StaticDepositPayment.static_deposit_address_id.in_(static_deposit_address_ids))
            .group_by(StaticDepositPayment.static_deposit_address_id)
        )
        return {
            static_deposit_address_id: StaticDepositAddressTotals(
                received_usd=Decimal(received_usd or 0).quantize(Decimal("0.01")),
                payment_count=payment_count,
                last_payment_at=last_payment_at,
            )
            for static_deposit_address_id, received_usd, payment_count, last_payment_at in rows
        }
