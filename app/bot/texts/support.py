"""Support texts, both sides."""

from datetime import datetime
from html import escape

from app.bot.texts.ru import format_date, format_left
from app.core.enums import PaymentStatus

PROMPT = (
    '💬 <b>Поддержка</b>\n\n'
    'Опишите проблему одним сообщением — можно приложить скриншот.\n'
    'Ответ придёт сюда же.'
)
SENT = (
    '✅ Сообщение отправлено. Обычно отвечаем в течение дня — '
    'ответ придёт прямо в этот чат.'
)
UNDELIVERED = (
    '⚠️ Не получилось отправить сообщение. Попробуйте ещё раз чуть позже.'
)
TOO_FAST = 'Слишком много сообщений подряд. Подождите минуту, пожалуйста.'
BLOCKED = 'Обращения с этого аккаунта отключены.'
LEFT = 'Вышли из поддержки. Пишите, если что-то понадобится.'

DEVICES_MORE = (
    'Мне нужно больше устройств. Сейчас подписка до {current} устройств.'
)
DEVICES_BEFORE_DOWNGRADE = (
    'Хочу купить тариф до {chosen} устройств, но сейчас оплачено до '
    '{current} устройств, срок — до {until}. Как лучше поступить?'
)

REPLY_HEADER = '💬 <b>Ответ поддержки</b>'

REPLY_DELIVERED = 'Отправлено пользователю {user_id}.'
REPLY_NO_THREAD = (
    'Не понимаю, кому это адресовано. Ответьте реплаем на карточку обращения.'
)
USER_BLOCKED = 'Пользователь {user_id} больше не может писать в поддержку.'
USER_UNBLOCKED = 'Пользователь {user_id} снова может писать в поддержку.'


def render_card(user, subscription, payments, now: datetime) -> str:
    """The context an admin needs before answering."""
    if user is None:
        return '👤 Неизвестный пользователь'

    # Names come from Telegram and may contain markup: escape them or a
    # user can break the card, or inject HTML into the admin's chat.
    name = escape(user.first_name or '—')
    username = f'@{escape(user.username)}' if user.username else 'без username'
    lines = [
        f'💬 <b>Обращение</b> от {name} · {username}',
        f'ID: <code>{user.id}</code>',
    ]

    if subscription is None:
        lines.append('Подписка: нет')
    else:
        lines.append(
            f'Подписка: {subscription.status} ({subscription.origin}) до '
            f'{format_date(subscription.expires_at)} — '
            f'{format_left(subscription.expires_at, now)}'
        )
        if subscription.subscription_token is None:
            lines.append('⚠️ не выдана в панели')

    paid = [p for p in payments if p.status == PaymentStatus.PROVISIONED]
    if paid:
        total = sum(p.amount_kopeks for p in paid) // 100
        lines.append(f'Платежей: {len(paid)} на {total} ₽')
    else:
        lines.append('Платежей нет')

    lines.append('')
    lines.append('Ответьте реплаем на это сообщение.')
    return '\n'.join(lines)


REPLY_BLOCKED = (
    'Не доставлено: пользователь {user_id} заблокировал бота. '
    'Он исключён из рассылок до следующего сообщения от него.'
)

STRAY = (
    'Я понимаю только кнопки 🙂\n\n'
    'Откройте меню — там подписка, оплата и поддержка.'
)
