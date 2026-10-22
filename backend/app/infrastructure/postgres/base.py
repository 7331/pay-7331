"""Declarative base and the ``pg_enum`` helper, plus ``BaseUnits``.

The base adds only the audit timestamps; each model declares its own primary key.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import DateTime, Dialect, Numeric, TypeDecorator, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def pg_enum_labels(enum_class: type[Enum]) -> list[str]:
    """The stored labels for an enum: its member names, lowercased."""
    return [member.name.lower() for member in enum_class]


def pg_enum(enum_class: type[Enum], *, name: str) -> SQLEnum:
    """A PostgreSQL native ENUM keyed by lowercased member name."""
    return SQLEnum(enum_class, name=name, values_callable=pg_enum_labels)


class BaseUnits(TypeDecorator[int]):
    """An asset amount in base units: ``numeric(78,0)`` (any uint256) in Postgres, ``int`` in Python, never a float."""

    impl = Numeric(78, 0)
    cache_ok = True

    def process_bind_param(self, value: int | None, dialect: Dialect) -> Decimal | None:  # noqa: ARG002, PLR0917  # SQLAlchemy's signature
        return None if value is None else Decimal(value)

    def process_result_value(self, value: Decimal | None, dialect: Dialect) -> int | None:  # noqa: ARG002, PLR0917  # SQLAlchemy's signature
        return None if value is None else int(value)


class Base(DeclarativeBase):
    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
