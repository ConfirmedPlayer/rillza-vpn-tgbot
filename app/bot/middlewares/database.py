"""Gives every handler its own unit of work.

One transaction per update: the handler works through ``data['uow']``
and commits explicitly. Anything left uncommitted is rolled back when
the update finishes, so a handler that raises cannot leave half-written
state behind.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.uow import UnitOfWork


class DatabaseMiddleware(BaseMiddleware):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with UnitOfWork(self._session_factory) as uow:
            data['uow'] = uow
            return await handler(event, data)
