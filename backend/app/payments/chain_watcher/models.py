"""How far the watcher has read each chain (PLAN.md section 11: ``chain_cursors``)."""

from enum import StrEnum

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base, pg_enum


class CursorStream(StrEnum):
    # The last finalized block whose readings were applied.
    FINALIZED_HEAD = "finalized_head"
    # DepositSwept logs are read up to here (always a finalized block).
    DEPOSIT_SWEPT_LOGS = "deposit_swept_logs"
    # Token Transfer logs to watched deposits are read up to here (a latest block).
    TOKEN_TRANSFER_LOGS = "token_transfer_logs"  # noqa: S105  # a stream name, not a secret


class ChainCursor(Base):
    __tablename__ = "chain_cursors"

    chain_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    stream_name: Mapped[CursorStream] = mapped_column(pg_enum(CursorStream, name="cursor_stream"), primary_key=True)
    last_block_number: Mapped[int] = mapped_column(BigInteger)
