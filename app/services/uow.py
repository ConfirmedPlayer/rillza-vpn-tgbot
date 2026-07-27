"""Unit of work: one session, all repositories, one transaction."""

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.broadcasts import BroadcastsRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.stats import StatsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.support import SupportRepository
from app.repositories.tariffs import TariffsRepository
from app.repositories.users import UsersRepository


class UnitOfWork:
    """Owns a session and rolls it back unless the block commits.

    Usage::

        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(telegram_id=1)
            await uow.commit()
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> 'UnitOfWork':
        self._session = self._session_factory()
        self.users = UsersRepository(self._session)
        self.tariffs = TariffsRepository(self._session)
        self.subscriptions = SubscriptionsRepository(self._session)
        self.payments = PaymentsRepository(self._session)
        self.broadcasts = BroadcastsRepository(self._session)
        self.stats = StatsRepository(self._session)
        self.support = SupportRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._session
        if session is None:
            return
        try:
            if exc_type is not None:
                await session.rollback()
        finally:
            await session.close()
            self._session = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError('UnitOfWork is not entered')
        return self._session

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
