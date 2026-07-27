"""Access filters."""

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.settings import Settings


class IsAdmin(Filter):
    """Passes only for ids listed in ADMIN_IDS."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(self, event: TelegramObject) -> bool:
        if isinstance(event, Message | CallbackQuery):
            user = event.from_user
            return user is not None and self._settings.is_admin(user.id)
        return False
