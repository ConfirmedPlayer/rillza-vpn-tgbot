"""Main menu, trial, subscription screen and connection guide."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.texts import ru
from app.core.enums import SubscriptionStatus
from app.core.settings import Settings
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.trial_service import TrialOutcome, TrialService


async def _edit(query: CallbackQuery, text: str, markup) -> None:
    """Edit in place, tolerating Telegram's "message is not modified"."""
    if query.message is None:
        return
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if 'message is not modified' not in str(error):
            raise


def render_subscription(view, now) -> str:
    subscription = view.subscription
    until = ru.format_date(subscription.expires_at)

    if subscription.status == SubscriptionStatus.REVOKED:
        return ru.SUBSCRIPTION_REVOKED
    if not subscription.is_active_at(now):
        return ru.SUBSCRIPTION_EXPIRED.format(until=until)

    text = ru.SUBSCRIPTION_ACTIVE.format(
        until=until, left=ru.format_left(subscription.expires_at, now)
    )
    if view.info is not None and view.info.traffic.total_bytes:
        text += ru.SUBSCRIPTION_TRAFFIC.format(
            used=ru.format_traffic(view.info.traffic.total_bytes)
        )
    if view.is_provisioned:
        text += ru.SUBSCRIPTION_HINT
    return text


async def show_menu(
    message: Message, trials: TrialService, telegram_id: int
) -> None:
    available = await trials.is_available(telegram_id)
    await message.answer(ru.START, reply_markup=keyboards.main_menu(available))


async def handle_start(
    message: Message, trials: TrialService, **_: object
) -> None:
    if message.from_user is None:
        return
    await show_menu(message, trials, message.from_user.id)


async def handle_menu(
    query: CallbackQuery, trials: TrialService, **_: object
) -> None:
    available = await trials.is_available(query.from_user.id)
    await _edit(query, ru.START, keyboards.main_menu(available))
    await query.answer()


async def handle_trial_offer(query: CallbackQuery, **_: object) -> None:
    await _edit(query, ru.TRIAL_OFFER, keyboards.trial_offer())
    await query.answer()


async def handle_trial_confirm(
    query: CallbackQuery,
    trials: TrialService,
    subscriptions: SubscriptionService,
    **_: object,
) -> None:
    result = await trials.grant(
        query.from_user.id, username=query.from_user.username or ''
    )

    if result.outcome is TrialOutcome.ALREADY_USED:
        await _edit(query, ru.TRIAL_ALREADY_USED, keyboards.back_to_menu())
    elif result.outcome is TrialOutcome.HAS_SUBSCRIPTION:
        await _edit(query, ru.TRIAL_HAS_SUBSCRIPTION, keyboards.back_to_menu())
    elif result.outcome is TrialOutcome.PENDING_PROVISIONING:
        await _edit(query, ru.PROVISIONING_DELAYED, keyboards.back_to_menu())
    else:
        assert result.subscription is not None
        url = subscriptions.subscription_url(result.subscription)
        await _edit(
            query,
            ru.TRIAL_GRANTED.format(
                until=ru.format_date(result.subscription.expires_at)
            ),
            keyboards.subscription(url),
        )
    await query.answer()


async def handle_subscription(
    query: CallbackQuery, subscriptions: SubscriptionService, **_: object
) -> None:
    view = await subscriptions.describe(query.from_user.id)
    if view is None:
        await _edit(query, ru.NO_SUBSCRIPTION, keyboards.back_to_menu())
        await query.answer()
        return

    await _edit(
        query,
        render_subscription(view, utcnow()),
        keyboards.subscription(view.url),
    )
    await query.answer()


async def handle_guide(
    query: CallbackQuery,
    subscriptions: SubscriptionService,
    settings: Settings,
    **_: object,
) -> None:
    view = await subscriptions.describe(query.from_user.id)
    url = view.url if view is not None else None
    text = ru.GUIDE if url else ru.GUIDE_NEEDS_SUBSCRIPTION
    await _edit(query, text, keyboards.guide(settings, url))
    await query.answer()


async def handle_support(query: CallbackQuery, **_: object) -> None:
    await _edit(query, ru.SUPPORT_PLACEHOLDER, keyboards.back_to_menu())
    await query.answer()


def build_router() -> Router:
    router = Router(name='menu')
    # Group and channel updates are dropped: this bot is private-chat only.
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    router.message.register(handle_start, CommandStart())
    router.callback_query.register(handle_menu, F.data == keyboards.MENU)
    router.callback_query.register(
        handle_trial_offer, F.data == keyboards.TRIAL_OFFER
    )
    router.callback_query.register(
        handle_trial_confirm, F.data == keyboards.TRIAL_CONFIRM
    )
    router.callback_query.register(
        handle_subscription, F.data == keyboards.SUBSCRIPTION
    )
    router.callback_query.register(handle_guide, F.data == keyboards.GUIDE)
    router.callback_query.register(handle_support, F.data == keyboards.SUPPORT)
    return router
