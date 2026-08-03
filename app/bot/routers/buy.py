"""Buying a subscription: tariff -> provider -> invoice -> check."""

import uuid

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery

from app.bot import keyboards
from app.bot.routers.menu import _edit
from app.bot.texts import ru
from app.core.settings import Settings
from app.integrations.payments import PaymentError, PaymentRegistry
from app.services.payment_service import (
    FinalizeOutcome,
    FinalizeResult,
    PaymentService,
)
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.uow import UnitOfWork


async def handle_buy(
    query: CallbackQuery, uow: UnitOfWork, **_: object
) -> None:
    counts = await uow.tariffs.list_device_counts()
    if not counts:
        await _edit(query, ru.BUY_NO_PROVIDERS, keyboards.back_to_menu())
        await query.answer()
        return
    await _edit(query, ru.BUY_CHOOSE_DEVICES, keyboards.devices(counts))
    await query.answer()


async def handle_devices(
    query: CallbackQuery, uow: UnitOfWork, **_: object
) -> None:
    raw = (query.data or '').removeprefix(keyboards.DEVICES_PREFIX)
    count_raw, _, _flag = raw.partition(':')
    try:
        count = int(count_raw)
    except ValueError:
        await query.answer(ru.PAYMENT_UNKNOWN, show_alert=True)
        return

    # Callback data is client-supplied and need not match a button the
    # bot drew, so the number is checked against what is on sale.
    if count not in await uow.tariffs.list_device_counts():
        await query.answer(ru.PAYMENT_UNKNOWN, show_alert=True)
        return

    confirmed = _flag == 'ok'
    subscription = await uow.subscriptions.get_by_user(query.from_user.id)
    now = utcnow()
    if (
        not confirmed
        and subscription is not None
        and subscription.is_active_at(now)
        and subscription.max_devices > count
    ):
        # is_active_at, not status == ACTIVE: an expired subscription
        # has nothing left to lose, so there is nothing to warn about.
        await _edit(
            query,
            ru.BUY_DOWNGRADE_WARNING.format(
                current=subscription.max_devices,
                chosen=count,
                until=ru.format_date(subscription.expires_at),
                left=ru.format_left(subscription.expires_at, now),
            ),
            keyboards.devices_downgrade(count, subscription.max_devices),
        )
        await query.answer()
        return

    tariffs = await uow.tariffs.list_active(count)
    await _edit(
        query,
        ru.BUY_CHOOSE_TARIFF.format(devices=count),
        keyboards.tariffs(tariffs),
    )
    await query.answer()


async def handle_tariff(
    query: CallbackQuery,
    uow: UnitOfWork,
    providers: PaymentRegistry,
    **_: object,
) -> None:
    tariff_id = int((query.data or '').removeprefix(keyboards.TARIFF_PREFIX))
    tariff = await uow.tariffs.get_sellable(tariff_id)
    if tariff is None:
        await query.answer(ru.PAYMENT_UNKNOWN, show_alert=True)
        return

    available = providers.available()
    if not available:
        await _edit(query, ru.BUY_NO_PROVIDERS, keyboards.back_to_menu())
        await query.answer()
        return

    await _edit(
        query,
        ru.BUY_CHOOSE_PROVIDER.format(
            tariff=tariff.title_ru, amount=tariff.price_rubles
        ),
        keyboards.providers(
            tariff.id, available, providers.title, tariff.max_devices
        ),
    )
    await query.answer()


async def handle_provider(
    query: CallbackQuery,
    uow: UnitOfWork,
    payments: PaymentService,
    settings: Settings,
    **_: object,
) -> None:
    raw = (query.data or '').removeprefix(keyboards.PROVIDER_PREFIX)
    tariff_id, _, provider_name = raw.partition(':')

    tariff = await uow.tariffs.get_sellable(int(tariff_id))
    if tariff is None:
        await query.answer(ru.PAYMENT_UNKNOWN, show_alert=True)
        return

    try:
        payment = await payments.create_invoice(
            query.from_user.id, tariff, provider_name
        )
    except PaymentError:
        await _edit(query, ru.PAYMENT_PROVIDER_DOWN, keyboards.back_to_menu())
        await query.answer()
        return

    await _edit(
        query,
        ru.INVOICE.format(
            amount=tariff.price_rubles,
            tariff=tariff.title_ru,
            ttl=settings.invoice_ttl_minutes,
        ),
        keyboards.invoice(
            payment.invoice_url or '', str(payment.id), tariff.price_rubles
        ),
    )
    await query.answer()


def _result_text(result: FinalizeResult) -> str:
    if result.outcome is FinalizeOutcome.PROVISIONED:
        return ru.PAYMENT_SUCCESS.format(
            until=ru.format_date(result.expires_at)
            if result.expires_at
            else '—'
        )
    if result.outcome is FinalizeOutcome.PAID_PENDING_PROVISIONING:
        return ru.PAYMENT_PAID_PROVISIONING
    if result.outcome is FinalizeOutcome.EXPIRED:
        return ru.PAYMENT_EXPIRED
    if result.outcome is FinalizeOutcome.UNKNOWN:
        return ru.PAYMENT_UNKNOWN
    if result.outcome is FinalizeOutcome.PROVIDER_UNAVAILABLE:
        return ru.PAYMENT_PROVIDER_DOWN
    return ru.PAYMENT_NOT_YET


async def handle_check(
    query: CallbackQuery,
    payments: PaymentService,
    subscriptions: SubscriptionService,
    **_: object,
) -> None:
    raw = (query.data or '').removeprefix(keyboards.CHECK_PREFIX)
    try:
        payment_id = uuid.UUID(raw)
    except ValueError:
        await query.answer(ru.PAYMENT_UNKNOWN, show_alert=True)
        return

    result = await payments.check_and_finalize(
        payment_id, telegram_id=query.from_user.id
    )

    if result.outcome is FinalizeOutcome.BUSY:
        # Another tap (or the poller) already holds the row: say so
        # instead of blocking the user behind an HTTP call.
        await query.answer(ru.PAYMENT_CHECKING)
        return

    if result.outcome is FinalizeOutcome.PROVISIONED:
        # Hand over what they just bought. The trial screen already
        # does this; a purchase used to end on «Главное меню» and leave
        # the buyer to go looking for their own subscription.
        view = await subscriptions.describe(query.from_user.id)
        markup = keyboards.subscription(view.url if view else None)
        await _edit(query, _result_text(result), markup)
        await query.answer()
        return

    if result.outcome is FinalizeOutcome.PAID_PENDING_PROVISIONING:
        await _edit(query, _result_text(result), keyboards.back_to_menu())
        await query.answer()
        return

    # Still unpaid or a transient failure: keep the invoice on screen.
    await query.answer(_result_text(result), show_alert=True)


def build_router() -> Router:
    router = Router(name='buy')
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    router.callback_query.register(handle_buy, F.data == keyboards.BUY)
    router.callback_query.register(
        handle_devices, F.data.startswith(keyboards.DEVICES_PREFIX)
    )
    router.callback_query.register(
        handle_tariff, F.data.startswith(keyboards.TARIFF_PREFIX)
    )
    router.callback_query.register(
        handle_provider, F.data.startswith(keyboards.PROVIDER_PREFIX)
    )
    router.callback_query.register(
        handle_check, F.data.startswith(keyboards.CHECK_PREFIX)
    )
    return router
