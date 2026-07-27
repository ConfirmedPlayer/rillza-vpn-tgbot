"""Keeps the users table in step with Telegram profiles.

Runs after :class:`DatabaseMiddleware`, so it reuses that update's unit
of work. It only refreshes profile fields — flags such as
``is_bot_blocked`` and ``trial_used_at`` are never touched here.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from app.services.uow import UnitOfWork


class UserUpsertMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user: User | None = data.get('event_from_user')
        uow: UnitOfWork | None = data.get('uow')

        if telegram_user is not None and uow is not None:
            data['user'] = await uow.users.upsert(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            await uow.commit()

        return await handler(event, data)
