"""Broadcast bookkeeping, so a restart resumes instead of re-sending."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import BroadcastStatus
from app.db.models import Broadcast


class BroadcastsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, broadcast_id: int) -> Broadcast | None:
        return await self._session.get(Broadcast, broadcast_id)

    async def add(self, broadcast: Broadcast) -> Broadcast:
        self._session.add(broadcast)
        await self._session.flush()
        return broadcast

    async def claim(self, broadcast_id: int) -> Broadcast | None:
        """draft -> running, once. A second tap on "Отправить" gets None."""
        result = await self._session.execute(
            update(Broadcast)
            .where(
                Broadcast.id == broadcast_id,
                Broadcast.status == BroadcastStatus.DRAFT,
            )
            .values(status=BroadcastStatus.RUNNING)
            .returning(Broadcast)
            .execution_options(
                synchronize_session=False, populate_existing=True
            )
        )
        return result.scalar_one_or_none()

    async def stale_running(self, before: datetime) -> Sequence[Broadcast]:
        """Broadcasts left mid-flight by a restart.

        ``run`` writes progress once per page, so a row whose
        ``updated_at`` has stopped moving has nobody working on it.
        """
        result = await self._session.execute(
            select(Broadcast)
            .where(
                Broadcast.status == BroadcastStatus.RUNNING,
                Broadcast.updated_at < before,
            )
            .order_by(Broadcast.id)
        )
        return result.scalars().all()

    async def running(self) -> Broadcast | None:
        result = await self._session.execute(
            select(Broadcast)
            .where(Broadcast.status == BroadcastStatus.RUNNING)
            .order_by(Broadcast.id)
            .limit(1)
        )
        return result.scalar_one_or_none()
