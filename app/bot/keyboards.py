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
TRIAL_OFFER = 'trial:offer'
TRIAL_CONFIRM = 'trial:confirm'
SUBSCRIPTION = 'subscription'
GUIDE = 'guide'
SUPPORT = 'support'


def main_menu(trial_available: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if trial_available:
        builder.button(text='🎁 3 дня бесплатно', callback_data=TRIAL_OFFER)
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
