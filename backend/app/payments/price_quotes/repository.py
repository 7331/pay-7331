"""Queries for price_quotes; no decisions."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.price_quotes.models import PriceQuote


class PriceQuoteRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def supersede_live_quotes(self, invoice_id: str, *, superseded_at: datetime) -> None:
        await self.session.execute(
            update(PriceQuote)
            .where(
                PriceQuote.invoice_id == invoice_id,
                PriceQuote.superseded_at.is_(None),
                PriceQuote.expires_at > superseded_at,
            )
            .values(superseded_at=superseded_at)
        )

    async def insert_quote(self, quote: PriceQuote) -> PriceQuote:
        self.session.add(quote)
        await self.session.flush()
        return quote

    async def list_quotes_for_invoice(self, invoice_id: str) -> list[PriceQuote]:
        return list(
            await self.session.scalars(
                select(PriceQuote).where(PriceQuote.invoice_id == invoice_id).order_by(PriceQuote.id)
            )
        )

    async def list_quotes_for_invoices(self, invoice_ids: list[str]) -> list[PriceQuote]:
        if not invoice_ids:
            return []
        return list(
            await self.session.scalars(
                select(PriceQuote).where(PriceQuote.invoice_id.in_(invoice_ids)).order_by(PriceQuote.id)
            )
        )

    async def find_latest_quote(self, invoice_id: str) -> PriceQuote | None:
        return await self.session.scalar(
            select(PriceQuote).where(PriceQuote.invoice_id == invoice_id).order_by(PriceQuote.id.desc()).limit(1)
        )
