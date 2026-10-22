"""Queries for payment_links; no decisions."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.payment_links.models import PaymentLink


class PaymentLinkRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def insert_payment_link(self, payment_link: PaymentLink) -> PaymentLink:
        self.session.add(payment_link)
        await self.session.flush()
        await self.session.refresh(payment_link)
        return payment_link

    async def find_payment_link(self, payment_link_id: str, *, lock: bool = False) -> PaymentLink | None:
        """``lock`` takes the row ``FOR UPDATE`` until the transaction ends: every payer's slot is counted and taken
        under it, so two payers can never both get the last one."""
        statement = select(PaymentLink).where(PaymentLink.id == payment_link_id)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self.session.scalar(statement)

    async def list_account_payment_links(self, account_id: int, *, limit: int) -> list[PaymentLink]:
        """The account's links, newest first."""
        return list(
            await self.session.scalars(
                select(PaymentLink)
                .where(PaymentLink.account_id == account_id)
                .order_by(PaymentLink.created_at.desc(), PaymentLink.id.desc())
                .limit(limit)
            )
        )
