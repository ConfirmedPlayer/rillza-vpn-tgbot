"""Builds per-update services on top of the update's unit of work.

Handlers ask for what they need by parameter name (``subscriptions``,
``trials``, ``settings``); nothing reaches for a global.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.core.settings import Settings
from app.integrations.celerity import CelerityClient
from app.services.subscription_service import SubscriptionService
from app.services.trial_service import TrialService
from app.services.uow import UnitOfWork


class ServicesMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, panel: CelerityClient) -> None:
        self._settings = settings
        self._panel = panel

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        uow: UnitOfWork | None = data.get('uow')
        if uow is not None:
            subscriptions = SubscriptionService(
                uow, self._panel, self._settings
            )
            data['subscriptions'] = subscriptions
            data['trials'] = TrialService(uow, subscriptions, self._settings)
        data['settings'] = self._settings
        data['panel'] = self._panel
        return await handler(event, data)
