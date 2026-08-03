"""User side of anonymous support."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.routers.menu import _edit
from app.bot.states import Support
from app.bot.texts import ru
from app.bot.texts import support as texts
from app.services.subscription_service import DEFAULT_MAX_DEVICES
from app.services.support_service import RelayOutcome, SupportService
from app.services.uow import UnitOfWork

OUTCOME_TEXTS = {
    RelayOutcome.SENT: texts.SENT,
    RelayOutcome.BLOCKED: texts.BLOCKED,
    RelayOutcome.TOO_FAST: texts.TOO_FAST,
    RelayOutcome.UNDELIVERED: texts.UNDELIVERED,
}


async def handle_stray(message: Message, **_: object) -> None:
    """Someone typed instead of tapping. Point them at the menu."""
    await message.answer(texts.STRAY, reply_markup=keyboards.back_to_menu())


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


async def handle_devices_request(
    query: CallbackQuery, uow: UnitOfWork, support: SupportService, **_: object
) -> None:
    """A canned ticket about device counts, carrying the numbers.

    Two questions share one handler: "I need more" from the
    subscription screen, and "I am about to buy fewer, how should I do
    it" from the downgrade warning, where nothing has been bought yet.
    The chosen count rides in the callback data of the second.
    """
    chosen = (query.data or '').removeprefix(f'{keyboards.SUPPORT_DEVICES}:')
    subscription = await uow.subscriptions.get_by_user(query.from_user.id)
    current = (
        subscription.max_devices
        if subscription is not None
        else DEFAULT_MAX_DEVICES
    )

    if chosen.isdigit() and subscription is not None:
        text = texts.DEVICES_BEFORE_DOWNGRADE.format(
            chosen=int(chosen),
            current=current,
            until=ru.format_date(subscription.expires_at),
        )
    else:
        text = texts.DEVICES_MORE.format(current=current)

    result = await support.relay_composed(query.from_user.id, text)
    if result.outcome is RelayOutcome.SENT:
        await query.answer(ru.SUPPORT_REQUEST_SENT, show_alert=True)
        return
    await query.answer(OUTCOME_TEXTS[result.outcome], show_alert=True)


def build_router() -> Router:
    router = Router(name='support')
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    router.callback_query.register(handle_open, F.data == keyboards.SUPPORT)
    router.callback_query.register(
        handle_devices_request, F.data.startswith(keyboards.SUPPORT_DEVICES)
    )
    router.message.register(handle_message, Support.writing)
    # Last of the user-side message handlers: a person who types
    # something outside the support flow used to get no answer at all,
    # which is indistinguishable from the bot being down.
    router.message.register(handle_stray)
    return router
