"""Broadcast bookkeeping, so a restart resumes instead of re-sending."""

from sqlalchemy import select
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

    async def running(self) -> Broadcast | None:
        result = await self._session.execute(
            select(Broadcast)
            .where(Broadcast.status == BroadcastStatus.RUNNING)
            .order_by(Broadcast.id)
            .limit(1)
        )
        return result.scalar_one_or_none()
