"""Inline keyboards.

Telegram only accepts http/https/tg links in buttons, so the one-tap
Happ import (``happ://add/...``) cannot live here — the panel's own
subscription page is the bridge, and "🔗 Открыть подписку" points at it
(PLAN.md §11).
"""

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.settings import Settings

MENU = 'menu'
BUY = 'buy'
TARIFF_PREFIX = 'tariff:'
PROVIDER_PREFIX = 'provider:'
CHECK_PREFIX = 'check:'
TRIAL_OFFER = 'trial:offer'
TRIAL_CONFIRM = 'trial:confirm'
SUBSCRIPTION = 'subscription'
GUIDE = 'guide'
SUPPORT = 'support'
RENEW = 'renew'

ADMIN_MENU = 'admin'
ADMIN_STATS = 'admin:stats'
ADMIN_TARIFFS = 'admin:tariffs'
ADMIN_BROADCAST = 'admin:broadcast'
ADMIN_BROADCAST_GO = 'admin:broadcast:go'
ADMIN_FIND_USER = 'admin:find'
ADMIN_USER_PREFIX = 'admin:user:'
ADMIN_GRANT_PREFIX = 'admin:grant:'
ADMIN_REVOKE_PREFIX = 'admin:revoke:'
ADMIN_RESYNC_PREFIX = 'admin:resync:'
ADMIN_RETRY_PREFIX = 'admin:retry:'


def main_menu(trial_available: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if trial_available:
        builder.button(text='🎁 3 дня бесплатно', callback_data=TRIAL_OFFER)
    builder.button(text='🛒 Купить подписку', callback_data=BUY)
    builder.button(text='🌐 Моя подписка', callback_data=SUBSCRIPTION)
    builder.button(text='📖 Как подключить', callback_data=GUIDE)
    builder.button(text='💬 Поддержка', callback_data=SUPPORT)
    builder.adjust(1)
    return builder.as_markup()


def trial_offer() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text='✅ Получить 3 дня', callback_data=TRIAL_CONFIRM)
    builder.button(text='↩️ Назад', callback_data=MENU)
    builder.adjust(1)
    return builder.as_markup()


def subscription(url: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if url is not None:
        builder.row(InlineKeyboardButton(text='🔗 Открыть подписку', url=url))
        builder.row(
            InlineKeyboardButton(
                text='📋 Скопировать ссылку',
                copy_text=CopyTextButton(text=url),
            )
        )
    builder.row(
        InlineKeyboardButton(text='📖 Как подключить', callback_data=GUIDE)
    )
    builder.row(
        InlineKeyboardButton(text='↩️ Главное меню', callback_data=MENU)
    )
    return builder.as_markup()


def guide(settings: Settings, url: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text='📱 Happ для iPhone / Mac', url=settings.happ_ios_url
        )
    )
    builder.row(
        InlineKeyboardButton(
            text='🤖 Happ для Android', url=settings.happ_android_url
        )
    )
    builder.row(
        InlineKeyboardButton(
            text='💻 Happ для Windows и других', url=settings.happ_site_url
        )
    )
    if url is not None:
        builder.row(InlineKeyboardButton(text='🔗 Открыть подписку', url=url))
        builder.row(
            InlineKeyboardButton(
                text='📋 Скопировать ссылку',
                copy_text=CopyTextButton(text=url),
            )
        )
    builder.row(
        InlineKeyboardButton(text='↩️ Главное меню', callback_data=MENU)
    )
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text='↩️ Главное меню', callback_data=MENU)
    return builder.as_markup()


def tariffs(items) -> InlineKeyboardMarkup:
    """One row per duration, with the per-month price to compare against."""
    builder = InlineKeyboardBuilder()
    # The shortest plan sets the reference month; longer ones show how
    # much cheaper their month is against it.
    reference = max((t.monthly_price_kopeks for t in items), default=0)
    for tariff in items:
        monthly = tariff.monthly_price_kopeks
        label = f'{tariff.title_ru} — {tariff.price_kopeks // 100} ₽'
        if reference and monthly < reference:
            discount = round((1 - monthly / reference) * 100)
            if discount:
                label += f' (выгода {discount}%)'
        builder.button(text=label, callback_data=f'{TARIFF_PREFIX}{tariff.id}')
    builder.button(text='↩️ Главное меню', callback_data=MENU)
    builder.adjust(1)
    return builder.as_markup()


def providers(tariff_id: int, names, titles) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for name in names:
        builder.button(
            text=titles(name),
            callback_data=f'{PROVIDER_PREFIX}{tariff_id}:{name}',
        )
    builder.button(text='↩️ Назад', callback_data=BUY)
    builder.adjust(1)
    return builder.as_markup()


def invoice(
    url: str, payment_id: str, amount_rubles: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f'💳 Оплатить {amount_rubles} ₽', url=url)
    )
    builder.row(
        InlineKeyboardButton(
            text='🧾 Я оплатил — проверить',
            callback_data=f'{CHECK_PREFIX}{payment_id}',
        )
    )
    builder.row(
        InlineKeyboardButton(text='↩️ Главное меню', callback_data=MENU)
    )
    return builder.as_markup()


def expiring_soon() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text='🛒 Продлить', callback_data=BUY)
    builder.button(text='🌐 Моя подписка', callback_data=SUBSCRIPTION)
    builder.adjust(1)
    return builder.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text='📊 Статистика', callback_data=ADMIN_STATS)
    builder.button(text='👤 Найти пользователя', callback_data=ADMIN_FIND_USER)
    builder.button(text='🧾 Тарифы', callback_data=ADMIN_TARIFFS)
    builder.button(text='📣 Рассылка', callback_data=ADMIN_BROADCAST)
    builder.adjust(1)
    return builder.as_markup()


def admin_back() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text='↩️ В админку', callback_data=ADMIN_MENU)
    return builder.as_markup()


def admin_user(
    telegram_id: int, has_stuck_payment: bool
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for days in (30, 90):
        builder.button(
            text=f'➕ {days} дней',
            callback_data=f'{ADMIN_GRANT_PREFIX}{telegram_id}:{days}',
        )
    builder.button(
        text='⛔️ Отозвать', callback_data=f'{ADMIN_REVOKE_PREFIX}{telegram_id}'
    )
    builder.button(
        text='🔄 Переподключить к серверам',
        callback_data=f'{ADMIN_RESYNC_PREFIX}{telegram_id}',
    )
    if has_stuck_payment:
        builder.button(
            text='♻️ Повторить провижининг',
            callback_data=f'{ADMIN_RETRY_PREFIX}{telegram_id}',
        )
    builder.button(text='↩️ В админку', callback_data=ADMIN_MENU)
    builder.adjust(2, 1, 1, 1, 1)
    return builder.as_markup()


def admin_broadcast_confirm() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text='📣 Отправить всем', callback_data=ADMIN_BROADCAST_GO)
    builder.button(text='↩️ Отмена', callback_data=ADMIN_MENU)
    builder.adjust(1)
    return builder.as_markup()
