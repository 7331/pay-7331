"""Queries for chain_cursors; no decisions."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.chain_watcher.models import ChainCursor, CursorStream


class ChainCursorRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def read_cursors(self, chain_key: str) -> dict[CursorStream, int]:
        rows = await self.session.scalars(select(ChainCursor).where(ChainCursor.chain_key == chain_key))
        return {row.stream_name: row.last_block_number for row in rows}

    async def write_cursor(self, chain_key: str, *, stream_name: CursorStream, last_block_number: int) -> None:
        await self.session.execute(
            insert(ChainCursor)
            .values(chain_key=chain_key, stream_name=stream_name, last_block_number=last_block_number)
            .on_conflict_do_update(
                index_elements=[ChainCursor.chain_key, ChainCursor.stream_name],
                set_={"last_block_number": last_block_number},
            )
        )
