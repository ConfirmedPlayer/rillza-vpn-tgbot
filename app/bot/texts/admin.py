"""Admin-facing texts and renderers."""

from datetime import datetime
from html import escape

from app.bot.texts.ru import format_date, format_left
from app.core.enums import PaymentStatus

MENU = '🛠 <b>Админка Rillza VPN</b>\n\nВыберите раздел.'
FIND_USER = '👤 Отправьте Telegram ID или @username пользователя.'
USER_NOT_FOUND = 'Пользователь не найден.'
PANEL_UNAVAILABLE = 'Панель не отвечает. Попробуйте ещё раз.'
GRANTED = 'Добавлено {days} дней.'
REVOKED = 'Доступ отозван.'
RESYNC_STARTED = (
    'Синхронизация запущена. Подключение появится в течение минуты.'
)
RETRY_DONE = 'Обработано зависших платежей: {count}.'

BROADCAST_PROMPT = (
    '📣 Отправьте сообщение для рассылки — текст, фото или что угодно.\n\n'
    'Оно уйдёт от имени бота: отправитель не виден.'
)
BROADCAST_CONFIRM = 'Получателей: <b>{count}</b>.\n\nОтправляем?'
BROADCAST_RUNNING = 'Рассылка запущена…'
BROADCAST_DONE = (
    '📣 Рассылка завершена.\n\n'
    'Доставлено: {sent}\nЗаблокировали бота: {blocked}\nОшибок: {failed}'
)
BROADCAST_LOST = 'Черновик потерян, начните заново.'


def rubles(kopeks: int) -> str:
    return f'{kopeks // 100} ₽'


def render_stats(stats, panel_stats, now: datetime) -> str:
    lines = [
        '📊 <b>Статистика</b>',
        '',
        f'Пользователей: <b>{stats.users}</b>',
        f'Активных подписок: <b>{stats.active_subscriptions}</b> '
        f'(из них триалов: {stats.trial_subscriptions})',
        f'Истёкших: {stats.expired_subscriptions}',
        f'Конверсия триала: <b>{stats.conversion_percent}%</b> '
        f'({stats.trial_converted} из {stats.trials_issued})',
        '',
        '<b>Выручка</b>',
        f'сутки: {rubles(stats.revenue_day_kopeks)}',
        f'неделя: {rubles(stats.revenue_week_kopeks)}',
        f'месяц: {rubles(stats.revenue_month_kopeks)}',
    ]

    if stats.payments_awaiting_provisioning:
        lines += [
            '',
            f'⚠️ Оплачено, но не выдано: '
            f'<b>{stats.payments_awaiting_provisioning}</b>',
        ]

    lines += ['', '<b>Панель</b>']
    if panel_stats is None:
        lines.append('❌ не отвечает')
    else:
        lines.append(
            f'ноды: {panel_stats.nodes_online} из {panel_stats.nodes_total} '
            f'онлайн, пользователей онлайн: {panel_stats.online_users}'
        )
        if panel_stats.offline_nodes:
            # An offline node silently vanishes from subscriptions.
            offline = ', '.join(panel_stats.offline_nodes)
            lines.append(f'⚠️ офлайн: {offline}')

    lines += ['', '<b>Фоновые задачи</b>']
    if not stats.heartbeats:
        lines.append('пока не запускались')
    for beat in stats.heartbeats:
        if beat.last_success_at is None:
            lines.append(f'{beat.job_name}: ❌ ни одного успеха')
            continue
        ago = int((now - beat.last_success_at).total_seconds() // 60)
        mark = '✅' if ago < 60 else '⚠️'
        line = f'{mark} {beat.job_name}: {ago} мин назад'
        if beat.last_error_at is not None:
            line += ' (были ошибки)'
        lines.append(line)

    return '\n'.join(lines)


def render_tariffs(tariffs) -> str:
    lines = ['🧾 <b>Тарифы</b>', '']
    for tariff in tariffs:
        state = '✅' if tariff.is_active else '⏸'
        lines.append(
            f'{state} <code>{tariff.code}</code> — {tariff.title_ru}, '
            f'{rubles(tariff.price_kopeks)} за {tariff.duration_days} дн. '
            f'({rubles(tariff.monthly_price_kopeks)}/мес)'
        )
    lines += ['', 'Цены меняются прямо в базе — деплой не нужен.']
    return '\n'.join(lines)


def render_user(user, subscription, payments, now: datetime) -> str:
    # Attacker-controlled: escape before it reaches HTML parse mode.
    name = escape(user.first_name or '—')
    username = f'@{escape(user.username)}' if user.username else '—'
    lines = [
        f'👤 <b>{name}</b> · {username}',
        f'ID: <code>{user.id}</code>',
        f'Регистрация: {format_date(user.created_at)}',
    ]
    if user.is_bot_blocked:
        lines.append('⛔️ заблокировал бота')
    if user.trial_used_at is not None:
        lines.append(f'Триал использован: {format_date(user.trial_used_at)}')

    lines.append('')
    if subscription is None:
        lines.append('Подписки нет.')
    else:
        lines.append(
            f'Подписка: <b>{subscription.status}</b> '
            f'({subscription.origin}) до '
            f'{format_date(subscription.expires_at)} — '
            f'{format_left(subscription.expires_at, now)}'
        )
        if subscription.subscription_token is None:
            lines.append('⚠️ не выдана в панели')

    lines += ['', '<b>Платежи</b>']
    if not payments:
        lines.append('нет')
    for payment in payments:
        mark = (
            '✅'
            if payment.status == PaymentStatus.PROVISIONED
            else ('⚠️' if payment.status == PaymentStatus.PAID else '·')
        )
        lines.append(
            f'{mark} {format_date(payment.created_at)} '
            f'{rubles(payment.amount_kopeks)} {payment.provider} '
            f'— {payment.status}'
        )
    return '\n'.join(lines)
