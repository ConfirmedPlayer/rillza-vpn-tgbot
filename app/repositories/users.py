"""User queries. No business rules live here — see services/."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


class UsersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, telegram_id: int) -> User | None:
        return await self._session.get(User, telegram_id)

    async def upsert(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        """Create the user or refresh their Telegram profile fields.

        Called on every incoming update, so it must not touch anything
        else on the row (notably not the bot-blocked flag).
        """
        statement = (
            insert(User)
            .values(id=telegram_id, username=username, first_name=first_name)
            .on_conflict_do_update(
                index_elements=[User.id],
                set_={'username': username, 'first_name': first_name},
            )
            .returning(User)
        )
        result = await self._session.execute(statement)
        return result.scalar_one()

    async def mark_trial_used(
        self, telegram_id: int, moment: datetime
    ) -> User | None:
        """Latch the trial; a second attempt gets None.

        The WHERE clause is the atomic guard against a double tap on
        "получить триал" issuing two trials.
        """
        result = await self._session.execute(
            update(User)
            .where(User.id == telegram_id, User.trial_used_at.is_(None))
            .values(trial_used_at=moment)
            .returning(User)
            .execution_options(
                synchronize_session=False, populate_existing=True
            )
        )
        return result.scalar_one_or_none()

    async def set_bot_blocked(self, telegram_id: int, blocked: bool) -> None:
        await self._session.execute(
            update(User)
            .where(User.id == telegram_id)
            .values(is_bot_blocked=blocked)
            .execution_options(synchronize_session='fetch')
        )

    async def set_support_blocked(
        self, telegram_id: int, moment: datetime | None
    ) -> None:
        await self._session.execute(
            update(User)
            .where(User.id == telegram_id)
            .values(support_blocked_at=moment)
            .execution_options(synchronize_session='fetch')
        )

    async def find_by_username(self, username: str) -> User | None:
        result = await self._session.execute(
            select(User).where(func.lower(User.username) == username.lower())
        )
        return result.scalars().first()

    async def count_reachable(self) -> int:
        """Users a broadcast can actually reach."""
        result = await self._session.execute(
            select(func.count(User.id)).where(User.is_bot_blocked.is_(False))
        )
        return result.scalar_one()

    async def count(self) -> int:
        result = await self._session.execute(select(func.count(User.id)))
        return result.scalar_one()

    async def iter_broadcast_targets(
        self, after_id: int | None = None, limit: int = 100
    ) -> Sequence[User]:
        """A page of users for a resumable broadcast, ordered by id."""
        statement = (
            select(User)
            .where(User.is_bot_blocked.is_(False))
            .order_by(User.id)
            .limit(limit)
        )
        if after_id is not None:
            statement = statement.where(User.id > after_id)
        result = await self._session.execute(statement)
        return result.scalars().all()
