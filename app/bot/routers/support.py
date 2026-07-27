"""User side of anonymous support."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.routers.menu import _edit
from app.bot.states import Support
from app.bot.texts import support as texts
from app.services.support_service import RelayOutcome, SupportService

OUTCOME_TEXTS = {
    RelayOutcome.SENT: texts.SENT,
    RelayOutcome.BLOCKED: texts.BLOCKED,
    RelayOutcome.TOO_FAST: texts.TOO_FAST,
    RelayOutcome.UNDELIVERED: texts.UNDELIVERED,
}


async def handle_open(
    query: CallbackQuery, state: FSMContext, **_: object
) -> None:
    await state.set_state(Support.writing)
    await _edit(query, texts.PROMPT, keyboards.support_writing())
    await query.answer()


async def handle_message(
    message: Message, support: SupportService, **_: object
) -> None:
    """Relay whatever the user sent — text, photo, document, voice."""
    if message.from_user is None:
        return

    result = await support.relay_from_user(
        telegram_id=message.from_user.id,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )
    await message.answer(
        OUTCOME_TEXTS[result.outcome], reply_markup=keyboards.support_writing()
    )


def build_router() -> Router:
    router = Router(name='support')
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    router.callback_query.register(handle_open, F.data == keyboards.SUPPORT)
    router.message.register(handle_message, Support.writing)
    return router
