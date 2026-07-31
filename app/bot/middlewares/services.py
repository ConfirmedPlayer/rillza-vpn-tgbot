"""Builds per-update services on top of the update's unit of work.

Handlers ask for what they need by parameter name (``subscriptions``,
``trials``, ``settings``); nothing reaches for a global.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.core.settings import Settings
from app.integrations.celerity import CelerityClient
from app.integrations.payments import PaymentRegistry
from app.services.broadcast_service import BroadcastService
from app.services.payment_service import PaymentService
from app.services.rate_limit import AllowAllRateLimiter, Cooldown, RateLimiter
from app.services.subscription_service import SubscriptionService
from app.services.support_service import SupportService
from app.services.trial_service import TrialService
from app.services.uow import UnitOfWork

#: A fleet-wide config re-push; the panel client asks for a cooldown.
SYNC_COOLDOWN = timedelta(minutes=2)


class ServicesMiddleware(BaseMiddleware):
    def __init__(
        self,
        settings: Settings,
        panel: CelerityClient,
        providers: PaymentRegistry,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._panel = panel
        self._providers = providers
        self._limiter = limiter or AllowAllRateLimiter()
        # One per process: a full fleet re-push is not a per-user action.
        self._sync_cooldown = Cooldown(SYNC_COOLDOWN)

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
            data['payments'] = PaymentService(
                uow, self._providers, subscriptions, self._settings
            )
            bot = data.get('bot')
            if bot is not None:
                data['broadcasts'] = BroadcastService(uow, bot)
                data['support'] = SupportService(
                    uow, bot, self._settings, subscriptions, self._limiter
                )
        data['settings'] = self._settings
        data['panel'] = self._panel
        data['providers'] = self._providers
        data['sync_cooldown'] = self._sync_cooldown
        return await handler(event, data)
