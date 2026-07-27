"""Declarative base and shared column helpers."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, MetaData, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core import enums

# Explicit naming so Alembic autogenerate produces stable constraint names.
NAMING_CONVENTION = {
    'ix': 'ix_%(table_name)s_%(column_0_N_name)s',
    'uq': 'uq_%(table_name)s_%(column_0_N_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_N_name)s',
    'pk': 'pk_%(table_name)s',
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """created_at / updated_at maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def enum_check(column: str, enum_class: type[StrEnum]) -> CheckConstraint:
    """CHECK constraint restricting ``column`` to the enum's values."""
    allowed = ', '.join(f"'{value}'" for value in enums.values(enum_class))
    return CheckConstraint(f'{column} IN ({allowed})', name=f'{column}_valid')


def enum_text(enum_class: type[StrEnum]) -> String:
    """TEXT column type sized to the widest member of ``enum_class``."""
    return String(max(len(value) for value in enums.values(enum_class)))
