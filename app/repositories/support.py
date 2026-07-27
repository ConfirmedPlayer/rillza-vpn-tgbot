"""Support thread lookups.

The mapping from an admin-side message to the user who wrote in is what
makes reply-routing work across restarts.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SupportDirection
from app.db.models import SupportMessage


class SupportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_recipient(
        self, admin_chat_id: int, admin_message_id: int
    ) -> int | None:
        """Who wrote the message an admin is replying to."""
        result = await self._session.execute(
            select(SupportMessage.user_id).where(
                SupportMessage.admin_chat_id == admin_chat_id,
                SupportMessage.admin_message_id == admin_message_id,
            )
        )
        return result.scalars().first()

    async def last_outbound_at(self, telegram_id: int) -> datetime | None:
        result = await self._session.execute(
            select(SupportMessage.created_at)
            .where(
                SupportMessage.user_id == telegram_id,
                SupportMessage.direction == SupportDirection.OUT,
            )
            .order_by(SupportMessage.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()
