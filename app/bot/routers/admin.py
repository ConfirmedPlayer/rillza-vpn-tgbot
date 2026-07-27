"""Admin panel: statistics, user management, tariffs, broadcasts."""

from datetime import timedelta

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.filters import IsAdmin
from app.bot.routers.menu import _edit
from app.bot.states import AdminBroadcast, AdminFindUser
from app.bot.texts import admin as texts
from app.core.enums import PaymentStatus
from app.core.settings import Settings
from app.integrations.celerity import CelerityClient, PanelError
from app.services.broadcast_service import BroadcastService
from app.services.payment_service import PaymentService
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.uow import UnitOfWork


async def handle_admin(message: Message, **_: object) -> None:
    await message.answer(texts.MENU, reply_markup=keyboards.admin_menu())


async def handle_ping(message: Message, **_: object) -> None:
    await message.answer('pong')


async def handle_menu(query: CallbackQuery, state: FSMContext, **_) -> None:
    await state.clear()
    await _edit(query, texts.MENU, keyboards.admin_menu())
    await query.answer()


async def handle_stats(
    query: CallbackQuery, uow: UnitOfWork, panel: CelerityClient, **_
) -> None:
    now = utcnow()
    stats = await uow.stats.collect(
        now,
        day=now - timedelta(days=1),
        week=now - timedelta(days=7),
        month=now - timedelta(days=30),
    )
    try:
        panel_stats = await panel.stats()
    except PanelError:
        panel_stats = None

    await _edit(
        query,
        texts.render_stats(stats, panel_stats, now),
        keyboards.admin_back(),
    )
    await query.answer()


async def handle_tariffs(query: CallbackQuery, uow: UnitOfWork, **_) -> None:
    tariffs = await uow.tariffs.list_all()
    await _edit(query, texts.render_tariffs(tariffs), keyboards.admin_back())
    await query.answer()


async def handle_find_user(
    query: CallbackQuery, state: FSMContext, **_
) -> None:
    await state.set_state(AdminFindUser.waiting_for_query)
    await _edit(query, texts.FIND_USER, keyboards.admin_back())
    await query.answer()


async def _show_user(
    target: Message | CallbackQuery,
    telegram_id: int,
    uow: UnitOfWork,
    subscriptions: SubscriptionService,
    edit: bool,
) -> None:
    user = await uow.users.get(telegram_id)
    if user is None:
        text, markup = texts.USER_NOT_FOUND, keyboards.admin_back()
    else:
        subscription = await subscriptions.get(telegram_id)
        payments = await uow.payments.list_by_user(telegram_id, limit=5)
        stuck = any(p.status == PaymentStatus.PAID for p in payments)
        text = texts.render_user(user, subscription, payments, utcnow())
        markup = keyboards.admin_user(telegram_id, stuck)

    if edit and isinstance(target, CallbackQuery):
        await _edit(target, text, markup)
    elif isinstance(target, Message):
        await target.answer(text, reply_markup=markup)


async def handle_user_query(
    message: Message,
    state: FSMContext,
    uow: UnitOfWork,
    subscriptions: SubscriptionService,
    **_,
) -> None:
    raw = (message.text or '').strip().lstrip('@')
    await state.clear()

    telegram_id: int | None = None
    if raw.isdigit():
        telegram_id = int(raw)
    else:
        found = await uow.users.find_by_username(raw)
        telegram_id = found.id if found is not None else None

    if telegram_id is None:
        await message.answer(
            texts.USER_NOT_FOUND, reply_markup=keyboards.admin_back()
        )
        return
    await _show_user(message, telegram_id, uow, subscriptions, edit=False)


async def handle_user_card(
    query: CallbackQuery,
    uow: UnitOfWork,
    subscriptions: SubscriptionService,
    **_,
) -> None:
    telegram_id = int(
        (query.data or '').removeprefix(keyboards.ADMIN_USER_PREFIX)
    )
    await _show_user(query, telegram_id, uow, subscriptions, edit=True)
    await query.answer()


async def handle_grant(
    query: CallbackQuery,
    uow: UnitOfWork,
    subscriptions: SubscriptionService,
    **_,
) -> None:
    raw = (query.data or '').removeprefix(keyboards.ADMIN_GRANT_PREFIX)
    telegram_id_raw, _, days_raw = raw.partition(':')
    telegram_id, days = int(telegram_id_raw), int(days_raw)

    now = utcnow()
    subscription = await subscriptions.get(telegram_id)
    try:
        if subscription is None:
            from app.core.enums import SubscriptionOrigin

            subscription = await subscriptions.create_pending(
                telegram_id,
                expires_at=now + timedelta(days=days),
                origin=SubscriptionOrigin.ADMIN_GRANT,
            )
            await subscriptions.provision(subscription)
        else:
            base = max(now, subscription.expires_at)
            await subscriptions.extend(
                subscription, base + timedelta(days=days)
            )
    except PanelError:
        await query.answer(texts.PANEL_UNAVAILABLE, show_alert=True)
        return

    await query.answer(texts.GRANTED.format(days=days))
    await _show_user(query, telegram_id, uow, subscriptions, edit=True)


async def handle_revoke(
    query: CallbackQuery,
    uow: UnitOfWork,
    subscriptions: SubscriptionService,
    **_,
) -> None:
    telegram_id = int(
        (query.data or '').removeprefix(keyboards.ADMIN_REVOKE_PREFIX)
    )
    subscription = await subscriptions.get(telegram_id)
    if subscription is None:
        await query.answer(texts.USER_NOT_FOUND, show_alert=True)
        return
    try:
        await subscriptions.revoke(subscription)
    except PanelError:
        await query.answer(texts.PANEL_UNAVAILABLE, show_alert=True)
        return

    await query.answer(texts.REVOKED)
    await _show_user(query, telegram_id, uow, subscriptions, edit=True)


async def handle_resync(
    query: CallbackQuery, panel: CelerityClient, **_
) -> None:
    try:
        await panel.sync()
    except PanelError:
        await query.answer(texts.PANEL_UNAVAILABLE, show_alert=True)
        return
    await query.answer(texts.RESYNC_STARTED, show_alert=True)


async def handle_retry_provisioning(
    query: CallbackQuery,
    uow: UnitOfWork,
    payments: PaymentService,
    subscriptions: SubscriptionService,
    **_,
) -> None:
    telegram_id = int(
        (query.data or '').removeprefix(keyboards.ADMIN_RETRY_PREFIX)
    )
    stuck = [
        payment
        for payment in await uow.payments.list_by_user(telegram_id, limit=20)
        if payment.status == PaymentStatus.PAID
    ]
    for payment in stuck:
        await payments.check_and_finalize(payment.id)

    await query.answer(texts.RETRY_DONE.format(count=len(stuck)))
    await _show_user(query, telegram_id, uow, subscriptions, edit=True)


async def handle_broadcast_start(
    query: CallbackQuery, state: FSMContext, **_
) -> None:
    await state.set_state(AdminBroadcast.waiting_for_message)
    await _edit(query, texts.BROADCAST_PROMPT, keyboards.admin_back())
    await query.answer()


async def handle_broadcast_draft(
    message: Message, state: FSMContext, uow: UnitOfWork, **_
) -> None:
    reachable = await uow.users.count_reachable()
    await state.update_data(
        chat_id=message.chat.id, message_id=message.message_id
    )
    await message.answer(
        texts.BROADCAST_CONFIRM.format(count=reachable),
        reply_markup=keyboards.admin_broadcast_confirm(),
    )


async def handle_broadcast_send(
    query: CallbackQuery,
    state: FSMContext,
    uow: UnitOfWork,
    broadcasts: BroadcastService,
    **_,
) -> None:
    data = await state.get_data()
    await state.clear()
    chat_id, message_id = data.get('chat_id'), data.get('message_id')
    if chat_id is None or message_id is None:
        await query.answer(texts.BROADCAST_LOST, show_alert=True)
        return

    broadcast = await broadcasts.create(int(chat_id), int(message_id))
    await _edit(query, texts.BROADCAST_RUNNING, keyboards.admin_back())
    await query.answer()

    report = await broadcasts.run(broadcast)
    if query.message is not None:
        await query.message.answer(
            texts.BROADCAST_DONE.format(
                sent=report.sent, blocked=report.blocked, failed=report.failed
            ),
            reply_markup=keyboards.admin_back(),
        )


def build_router(settings: Settings) -> Router:
    router = Router(name='admin')
    is_admin = IsAdmin(settings)
    router.message.filter(F.chat.type == ChatType.PRIVATE, is_admin)
    router.callback_query.filter(is_admin)

    router.message.register(handle_admin, Command('admin'))
    router.message.register(handle_ping, Command('ping'))
    router.message.register(handle_user_query, AdminFindUser.waiting_for_query)
    router.message.register(
        handle_broadcast_draft, AdminBroadcast.waiting_for_message
    )

    router.callback_query.register(handle_menu, F.data == keyboards.ADMIN_MENU)
    router.callback_query.register(
        handle_stats, F.data == keyboards.ADMIN_STATS
    )
    router.callback_query.register(
        handle_tariffs, F.data == keyboards.ADMIN_TARIFFS
    )
    router.callback_query.register(
        handle_find_user, F.data == keyboards.ADMIN_FIND_USER
    )
    router.callback_query.register(
        handle_broadcast_start, F.data == keyboards.ADMIN_BROADCAST
    )
    router.callback_query.register(
        handle_broadcast_send, F.data == keyboards.ADMIN_BROADCAST_GO
    )
    router.callback_query.register(
        handle_user_card, F.data.startswith(keyboards.ADMIN_USER_PREFIX)
    )
    router.callback_query.register(
        handle_grant, F.data.startswith(keyboards.ADMIN_GRANT_PREFIX)
    )
    router.callback_query.register(
        handle_revoke, F.data.startswith(keyboards.ADMIN_REVOKE_PREFIX)
    )
    router.callback_query.register(
        handle_resync, F.data.startswith(keyboards.ADMIN_RESYNC_PREFIX)
    )
    router.callback_query.register(
        handle_retry_provisioning,
        F.data.startswith(keyboards.ADMIN_RETRY_PREFIX),
    )
    return router
