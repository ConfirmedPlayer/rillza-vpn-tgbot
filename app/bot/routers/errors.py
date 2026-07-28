"""Last line of defence for handler failures.

Without this an exception inside a handler leaves the user staring at a
spinning button: Telegram keeps the callback "in progress" until it is
answered, and a stale callback (older than 48 hours) or a malformed one
would take the whole update down silently.
"""

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, ErrorEvent
from loguru import logger

from app.bot import keyboards
from app.bot.texts import ru


async def handle_error(event: ErrorEvent) -> bool:
    logger.exception(
        'Update {} failed: {}',
        getattr(event.update, 'update_id', '?'),
        event.exception,
    )

    query = event.update.callback_query
    if isinstance(query, CallbackQuery):
        # Always release the spinner, then offer a way out.
        try:
            await query.answer(ru.SOMETHING_WENT_WRONG, show_alert=True)
        except TelegramBadRequest:
            # Too old to answer — nothing more we can do for it.
            pass
        if query.message is not None:
            try:
                await query.message.answer(
                    ru.SOMETHING_WENT_WRONG,
                    reply_markup=keyboards.back_to_menu(),
                )
            except TelegramBadRequest:
                pass
    elif event.update.message is not None:
        try:
            await event.update.message.answer(
                ru.SOMETHING_WENT_WRONG, reply_markup=keyboards.back_to_menu()
            )
        except TelegramBadRequest:
            pass

    # Handled: do not let it bubble into the polling loop.
    return True


def build_router() -> Router:
    router = Router(name='errors')
    router.errors.register(handle_error)
    return router
