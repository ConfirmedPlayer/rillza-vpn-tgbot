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
from app.bot.states import AdminBroadcast, AdminFindUser, AdminTariffPrice
from app.bot.texts import admin as texts
from app.bot.texts import support as support_texts
from app.core.enums import PaymentStatus
from app.core.settings import Settings
from app.integrations.celerity import CelerityClient, PanelError
from app.services.broadcast_service import BroadcastService
from app.services.payment_service import FinalizeOutcome, PaymentService
from app.services.rate_limit import Cooldown
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.support_service import SupportService
from app.services.uow import UnitOfWork

#: How far back the user card and the retry button both look.
RECENT_PAYMENTS = 20


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


async def handle_tariffs(
    query: CallbackQuery, uow: UnitOfWork, state: FSMContext, **_
) -> None:
    await state.clear()
    tariffs = await uow.tariffs.list_all()
    await _edit(
        query, texts.render_tariffs(tariffs), keyboards.admin_tariffs(tariffs)
    )
    await query.answer()


async def _show_tariff(
    query: CallbackQuery, tariff_id: int, uow: UnitOfWork
) -> None:
    tariff = await uow.tariffs.get(tariff_id)
    if tariff is None:
        await query.answer(texts.USER_NOT_FOUND, show_alert=True)
        return
    await _edit(
        query, texts.render_tariff(tariff), keyboards.admin_tariff(tariff)
    )


async def handle_tariff_card(
    query: CallbackQuery, uow: UnitOfWork, state: FSMContext, **_
) -> None:
    await state.clear()
    tariff_id = int(
        (query.data or '').removeprefix(keyboards.ADMIN_TARIFF_PREFIX)
    )
    await _show_tariff(query, tariff_id, uow)
    await query.answer()


async def handle_tariff_price(
    query: CallbackQuery, uow: UnitOfWork, state: FSMContext, **_
) -> None:
    tariff_id = int(
        (query.data or '').removeprefix(keyboards.ADMIN_TARIFF_PRICE_PREFIX)
    )
    tariff = await uow.tariffs.get(tariff_id)
    if tariff is None:
        await query.answer(texts.USER_NOT_FOUND, show_alert=True)
        return
    await state.set_state(AdminTariffPrice.waiting_for_price)
    await state.update_data(tariff_id=tariff_id)
    await _edit(
        query,
        texts.TARIFF_ASK_PRICE.format(
            current=texts.rubles(tariff.price_kopeks)
        ),
        keyboards.admin_tariff(tariff),
    )
    await query.answer()


async def handle_tariff_new_price(
    message: Message, state: FSMContext, uow: UnitOfWork, **_
) -> None:
    raw = (message.text or '').strip()
    if not raw.isdigit() or int(raw) <= 0:
        # Stay in the state: the admin retypes rather than starting over.
        await message.answer(texts.TARIFF_BAD_PRICE)
        return

    data = await state.get_data()
    tariff_id = data.get('tariff_id')
    await state.clear()
    if tariff_id is None:
        await message.answer(
            texts.USER_NOT_FOUND, reply_markup=keyboards.admin_back()
        )
        return

    # Rubles in, kopeks stored: money never becomes a float.
    updated = await uow.tariffs.set_price(int(tariff_id), int(raw) * 100)
    await uow.commit()
    if updated is None:
        await message.answer(
            texts.USER_NOT_FOUND, reply_markup=keyboards.admin_back()
        )
        return
    await message.answer(
        texts.render_tariff(updated),
        reply_markup=keyboards.admin_tariff(updated),
    )


async def handle_tariff_toggle(
    query: CallbackQuery, uow: UnitOfWork, state: FSMContext, **_
) -> None:
    await state.clear()
    tariff_id = int(
        (query.data or '').removeprefix(keyboards.ADMIN_TARIFF_TOGGLE_PREFIX)
    )
    tariff = await uow.tariffs.get(tariff_id)
    if tariff is None:
        await query.answer(texts.USER_NOT_FOUND, show_alert=True)
        return

    updated = await uow.tariffs.set_active(tariff_id, not tariff.is_active)
    await uow.commit()
    if updated is None:  # pragma: no cover - it existed a line ago
        await query.answer(texts.USER_NOT_FOUND, show_alert=True)
        return
    await query.answer(
        texts.TARIFF_ENABLED if updated.is_active else texts.TARIFF_DISABLED
    )
    await _edit(
        query, texts.render_tariff(updated), keyboards.admin_tariff(updated)
    )


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
        # Same window the retry button acts on, so the button cannot be
        # missing for a payment the retry would in fact have picked up.
        payments = await uow.payments.list_by_user(
            telegram_id, limit=RECENT_PAYMENTS
        )
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
    query: CallbackQuery, panel: CelerityClient, sync_cooldown: Cooldown, **_
) -> None:
    if not sync_cooldown.claim():
        # POST /api/sync re-pushes every config to every node, and the
        # button can be held down. The panel client's own docstring
        # asks for this gate.
        await query.answer(texts.RESYNC_TOO_SOON, show_alert=True)
        return
    try:
        await panel.sync()
    except PanelError:
        sync_cooldown.release()
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
        for payment in await uow.payments.list_by_user(
            telegram_id, limit=RECENT_PAYMENTS
        )
        if payment.status == PaymentStatus.PAID
    ]
    delivered = 0
    for payment in stuck:
        result = await payments.check_and_finalize(payment.id)
        if result.outcome is FinalizeOutcome.PROVISIONED:
            delivered += 1

    # Attempts are not outcomes: with the panel down this used to say
    # "обработано 3" while nothing at all had been handed over.
    await query.answer(
        texts.RETRY_DONE.format(count=delivered, total=len(stuck))
    )
    await _show_user(query, telegram_id, uow, subscriptions, edit=True)


async def handle_support_reply(
    message: Message, support: SupportService, **_
) -> None:
    """A reply to a support card goes back to whoever wrote in.

    The answer is copied, so the user sees it from the bot and never
    learns who is behind it.
    """
    reply_to = message.reply_to_message
    if reply_to is None:
        return

    recipient = await support.relay_to_user(
        admin_chat_id=message.chat.id,
        reply_to_message_id=reply_to.message_id,
        message_id=message.message_id,
    )
    if recipient is None:
        await message.reply(support_texts.REPLY_NO_THREAD)
        return
    await message.reply(
        support_texts.REPLY_DELIVERED.format(user_id=recipient)
    )


async def handle_support_block(
    query: CallbackQuery, support: SupportService, **_
) -> None:
    telegram_id = int(
        (query.data or '').removeprefix(keyboards.SUPPORT_BLOCK_PREFIX)
    )
    await support.set_blocked(telegram_id, True)
    await query.answer(
        support_texts.USER_BLOCKED.format(user_id=telegram_id), show_alert=True
    )
    if query.message is not None:
        await query.message.edit_reply_markup(
            reply_markup=keyboards.support_blocked(telegram_id)
        )


async def handle_support_unblock(
    query: CallbackQuery, support: SupportService, **_
) -> None:
    telegram_id = int(
        (query.data or '').removeprefix(keyboards.SUPPORT_UNBLOCK_PREFIX)
    )
    await support.set_blocked(telegram_id, False)
    await query.answer(
        support_texts.USER_UNBLOCKED.format(user_id=telegram_id),
        show_alert=True,
    )
    if query.message is not None:
        await query.message.edit_reply_markup(
            reply_markup=keyboards.support_card(telegram_id)
        )


async def handle_broadcast_start(
    query: CallbackQuery, state: FSMContext, **_
) -> None:
    await state.set_state(AdminBroadcast.waiting_for_message)
    await _edit(query, texts.BROADCAST_PROMPT, keyboards.admin_back())
    await query.answer()


async def handle_broadcast_draft(
    message: Message,
    state: FSMContext,
    uow: UnitOfWork,
    broadcasts: BroadcastService,
    **_,
) -> None:
    reachable = await uow.users.count_reachable()
    draft = await broadcasts.create(message.chat.id, message.message_id)
    # The draft is captured; leave the flow immediately. Staying in it
    # made every later admin message a new draft — including a reply to
    # a support card, which was then swallowed instead of delivered, and
    # which any still-visible confirm button would have broadcast.
    await state.clear()
    await message.answer(
        texts.BROADCAST_CONFIRM.format(count=reachable),
        reply_markup=keyboards.admin_broadcast_confirm(draft.id),
    )


async def handle_broadcast_send(
    query: CallbackQuery, uow: UnitOfWork, broadcasts: BroadcastService, **_
) -> None:
    raw = (query.data or '').removeprefix(keyboards.ADMIN_BROADCAST_GO_PREFIX)
    try:
        # The id comes from the card that was tapped, so an old card
        # sends its own draft and nothing else.
        broadcast_id = int(raw)
    except ValueError:
        await query.answer(texts.BROADCAST_LOST, show_alert=True)
        return

    # Claiming is the guard against a double tap: only one wins.
    broadcast = await broadcasts.claim(broadcast_id)
    if broadcast is None:
        await query.answer(texts.BROADCAST_ALREADY_RUNNING, show_alert=True)
        return

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
        handle_tariff_new_price, AdminTariffPrice.waiting_for_price
    )
    router.message.register(
        handle_broadcast_draft, AdminBroadcast.waiting_for_message
    )
    # Registered after the stateful handlers: a reply only means "answer
    # this user" when the admin is not in the middle of another flow.
    router.message.register(handle_support_reply, F.reply_to_message)

    router.callback_query.register(handle_menu, F.data == keyboards.ADMIN_MENU)
    router.callback_query.register(
        handle_stats, F.data == keyboards.ADMIN_STATS
    )
    router.callback_query.register(
        handle_tariffs, F.data == keyboards.ADMIN_TARIFFS
    )
    router.callback_query.register(
        handle_tariff_price,
        F.data.startswith(keyboards.ADMIN_TARIFF_PRICE_PREFIX),
    )
    router.callback_query.register(
        handle_tariff_toggle,
        F.data.startswith(keyboards.ADMIN_TARIFF_TOGGLE_PREFIX),
    )
    router.callback_query.register(
        handle_tariff_card, F.data.startswith(keyboards.ADMIN_TARIFF_PREFIX)
    )
    router.callback_query.register(
        handle_find_user, F.data == keyboards.ADMIN_FIND_USER
    )
    router.callback_query.register(
        handle_broadcast_start, F.data == keyboards.ADMIN_BROADCAST
    )
    router.callback_query.register(
        handle_broadcast_send,
        F.data.startswith(keyboards.ADMIN_BROADCAST_GO_PREFIX),
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
    router.callback_query.register(
        handle_support_block, F.data.startswith(keyboards.SUPPORT_BLOCK_PREFIX)
    )
    router.callback_query.register(
        handle_support_unblock,
        F.data.startswith(keyboards.SUPPORT_UNBLOCK_PREFIX),
    )
    return router
