"""Queries the open-invoice caps count (``creation_limits.py``), split out of ``repository.py``; no decisions."""

from datetime import datetime

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.invoices.enums import InvoiceStatus
from app.payments.invoices.models import Invoice


class InvoiceCreationLimitRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def lock_client_ip_hash(self, client_ip_hash: str) -> None:
        """Serialize one client network's invoice creations until the transaction ends, so the open-invoice cap holds
        in a burst."""
        await self.session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(client_ip_hash, 0))))

    async def count_open_invoices_for_account(self, account_id: int) -> int:
        count = await self.session.scalar(
            select(func.count()).where(Invoice.account_id == account_id, Invoice.closed_at.is_(None))
        )
        return count or 0

    async def count_open_invoices_for_ip_hash(self, client_ip_hash: str) -> int:
        count = await self.session.scalar(
            select(func.count()).where(Invoice.client_ip_hash == client_ip_hash, Invoice.closed_at.is_(None))
        )
        return count or 0

    async def count_open_invoices_for_ipv6_48_hash(self, client_ipv6_48_hash: str) -> int:
        count = await self.session.scalar(
            select(func.count()).where(Invoice.client_ipv6_48_hash == client_ipv6_48_hash, Invoice.closed_at.is_(None))
        )
        return count or 0

    async def count_open_public_invoices_by_chain(self, chain_keys: list[str]) -> dict[str, int]:
        """Invoices per chain that a public route made (signed out, or by a quantity link's payer) and that still
        wait for a coin or a payment, so each closes on its own clock: an underpaid public one stays open for top-ups
        with no end, and dust would hold the cap shut for good. Over ``ix_invoices_chain_key_open``."""
        rows = await self.session.execute(
            select(Invoice.chain_key, func.count())
            .where(
                Invoice.chain_key.in_(chain_keys),
                Invoice.closed_at.is_(None),
                Invoice.status.in_([InvoiceStatus.AWAITING_ASSET, InvoiceStatus.AWAITING_PAYMENT]),
                or_(Invoice.account_id.is_(None), Invoice.payment_link_id.is_not(None)),
            )
            .group_by(Invoice.chain_key)
        )
        return dict(rows.tuples().all())

    async def find_soonest_clock_close_for_ip_hash(self, client_ip_hash: str) -> datetime | None:
        """When the first of the network's open invoices closes by the clock alone: an unpaid one at ``expires_at``,
        an underpaid one at ``underpaid_open_until``. None when none of them has a clock (confirming, paid and not yet
        swept, or underpaid and open for top-ups without an end)."""
        return await self.find_soonest_clock_close(Invoice.client_ip_hash == client_ip_hash)

    async def find_soonest_clock_close_for_ipv6_48_hash(self, client_ipv6_48_hash: str) -> datetime | None:
        """``find_soonest_clock_close_for_ip_hash`` over a whole IPv6 /48."""
        return await self.find_soonest_clock_close(Invoice.client_ipv6_48_hash == client_ipv6_48_hash)

    async def find_soonest_clock_close(self, client_network_condition: ColumnElement[bool]) -> datetime | None:
        clock_close_time = case(
            (
                Invoice.status.in_([InvoiceStatus.AWAITING_ASSET, InvoiceStatus.AWAITING_PAYMENT]),
                Invoice.expires_at,
            ),
            (Invoice.status == InvoiceStatus.UNDERPAID, Invoice.underpaid_open_until),
            else_=None,
        )
        return await self.session.scalar(
            select(func.min(clock_close_time)).where(client_network_condition, Invoice.closed_at.is_(None))
        )
