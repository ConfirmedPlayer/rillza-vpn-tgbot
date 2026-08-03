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
DEVICES_PREFIX = 'devices:'
TARIFF_PREFIX = 'tariff:'
PROVIDER_PREFIX = 'provider:'
CHECK_PREFIX = 'check:'
TRIAL_OFFER = 'trial:offer'
TRIAL_CONFIRM = 'trial:confirm'
SUBSCRIPTION = 'subscription'
GUIDE = 'guide'
SUPPORT = 'support'
RENEW = 'renew'
SUPPORT_LEAVE = 'support:leave'
SUPPORT_BLOCK_PREFIX = 'support:block:'
SUPPORT_UNBLOCK_PREFIX = 'support:unblock:'

ADMIN_MENU = 'admin'
ADMIN_STATS = 'admin:stats'
ADMIN_TARIFFS = 'admin:tariffs'
ADMIN_TARIFF_PREFIX = 'admin:tariff:'
ADMIN_TARIFF_PRICE_PREFIX = 'admin:tprice:'
ADMIN_TARIFF_TOGGLE_PREFIX = 'admin:ttoggle:'
ADMIN_BROADCAST = 'admin:broadcast'
#: The draft id rides in the callback data. Keeping it in FSM state
#: instead meant a confirm button sent whatever draft was newest —
#: a card and the message it promises to send must be one thing.
ADMIN_BROADCAST_GO_PREFIX = 'admin:broadcast:go:'
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


def devices(counts) -> InlineKeyboardMarkup:
    """Step one of a purchase: how many devices."""
    builder = InlineKeyboardBuilder()
    for count in counts:
        builder.button(
            text=f'👥 До {count} устройств',
            callback_data=f'{DEVICES_PREFIX}{count}',
        )
    builder.button(text='↩️ Главное меню', callback_data=MENU)
    builder.adjust(1)
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
    builder.button(text='↩️ Назад', callback_data=BUY)
    builder.adjust(1)
    return builder.as_markup()


def providers(
    tariff_id: int, names, titles, max_devices: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for name in names:
        builder.button(
            text=titles(name),
            callback_data=f'{PROVIDER_PREFIX}{tariff_id}:{name}',
        )
    # Back goes to the tariff list for the device count the buyer
    # chose, not to the device-count screen — same target the tariff
    # screen's own back button uses (`tariffs()` above).
    builder.button(
        text='↩️ Назад', callback_data=f'{DEVICES_PREFIX}{max_devices}'
    )
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


def admin_broadcast_confirm(broadcast_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text='📣 Отправить всем',
        callback_data=f'{ADMIN_BROADCAST_GO_PREFIX}{broadcast_id}',
    )
    builder.button(text='↩️ Отмена', callback_data=ADMIN_MENU)
    builder.adjust(1)
    return builder.as_markup()


def support_writing() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text='↩️ Выйти из поддержки', callback_data=MENU)
    return builder.as_markup()


def support_card(telegram_id: int) -> InlineKeyboardMarkup:
    """Admin-side actions on an incoming support message."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text='👤 Профиль', callback_data=f'{ADMIN_USER_PREFIX}{telegram_id}'
    )
    builder.button(
        text='🚫 Заблокировать',
        callback_data=f'{SUPPORT_BLOCK_PREFIX}{telegram_id}',
    )
    builder.adjust(2)
    return builder.as_markup()


def support_blocked(telegram_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text='♻️ Разблокировать',
        callback_data=f'{SUPPORT_UNBLOCK_PREFIX}{telegram_id}',
    )
    return builder.as_markup()


def admin_tariffs(items) -> InlineKeyboardMarkup:
    """One button per tariff: prices are edited here, not in psql."""
    builder = InlineKeyboardBuilder()
    for tariff in items:
        state = '✅' if tariff.is_active else '⏸'
        label = f'{state} {tariff.title_ru} — {tariff.price_kopeks // 100} ₽'
        builder.row(
            InlineKeyboardButton(
                text=label, callback_data=f'{ADMIN_TARIFF_PREFIX}{tariff.id}'
            )
        )
    builder.row(InlineKeyboardButton(text='↩️ Назад', callback_data=ADMIN_MENU))
    return builder.as_markup()


def admin_tariff(tariff) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text='💰 Изменить цену',
            callback_data=f'{ADMIN_TARIFF_PRICE_PREFIX}{tariff.id}',
        )
    )
    builder.row(
        InlineKeyboardButton(
            text='⏸ Убрать из продажи'
            if tariff.is_active
            else '✅ Вернуть в продажу',
            callback_data=f'{ADMIN_TARIFF_TOGGLE_PREFIX}{tariff.id}',
        )
    )
    builder.row(
        InlineKeyboardButton(text='↩️ К тарифам', callback_data=ADMIN_TARIFFS)
    )
    return builder.as_markup()
