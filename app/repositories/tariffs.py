"""Tariff queries.

Writes RETURN the updated row, like the other repositories: reading an
ORM object back after a plain UPDATE is unreliable in async code.
"""

from collections.abc import Sequence

from sqlalchemy import Update, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tariff


def _returning(statement: Update) -> Update:
    return statement.returning(Tariff).execution_options(
        synchronize_session=False, populate_existing=True
    )


class TariffsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tariff_id: int) -> Tariff | None:
        return await self._session.get(Tariff, tariff_id)

    async def get_sellable(self, tariff_id: int) -> Tariff | None:
        """A tariff the shop may still invoice.

        ``get`` fetches by primary key alone, which is right for reading
        history — a payment must resolve its tariff long after the plan
        was withdrawn. It is wrong for selling: callback data comes from
        the client and need not match any button the bot drew, so a
        retired promo would stay purchasable at its old price forever.
        """
        result = await self._session.execute(
            select(Tariff).where(
                Tariff.id == tariff_id,
                Tariff.is_active.is_(True),
                Tariff.is_archived.is_(False),
            )
        )
        return result.scalar_one_or_none()

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

    async def set_price(
        self, tariff_id: int, price_kopeks: int
    ) -> Tariff | None:
        """Kopeks only: money never becomes a float on the way in."""
        result = await self._session.execute(
            _returning(
                update(Tariff)
                .where(Tariff.id == tariff_id)
                .values(price_kopeks=price_kopeks)
            )
        )
        return result.scalar_one_or_none()

    async def set_active(self, tariff_id: int, active: bool) -> Tariff | None:
        """Show or hide a tariff. Archiving stays a separate concern:
        payments reference tariffs, so rows are never deleted."""
        result = await self._session.execute(
            _returning(
                update(Tariff)
                .where(Tariff.id == tariff_id)
                .values(is_active=active)
            )
        )
        return result.scalar_one_or_none()
