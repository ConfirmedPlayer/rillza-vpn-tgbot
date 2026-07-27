"""Tariff queries."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tariff


class TariffsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tariff_id: int) -> Tariff | None:
        return await self._session.get(Tariff, tariff_id)

    async def get_by_code(self, code: str) -> Tariff | None:
        result = await self._session.execute(
            select(Tariff).where(Tariff.code == code)
        )
        return result.scalar_one_or_none()

    async def list_active(self) -> Sequence[Tariff]:
        """What the purchase screen shows, cheapest period first."""
        result = await self._session.execute(
            select(Tariff)
            .where(Tariff.is_active.is_(True), Tariff.is_archived.is_(False))
            .order_by(Tariff.sort_order, Tariff.duration_days)
        )
        return result.scalars().all()

    async def list_all(self) -> Sequence[Tariff]:
        """Everything except archived rows — the admin tariff screen."""
        result = await self._session.execute(
            select(Tariff)
            .where(Tariff.is_archived.is_(False))
            .order_by(Tariff.sort_order, Tariff.duration_days)
        )
        return result.scalars().all()
