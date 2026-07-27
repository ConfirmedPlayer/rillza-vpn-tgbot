"""User-facing Russian texts.

Kept in one module so wording can be reviewed without reading handlers.
Two rules from PLAN.md shape the copy: device limits are described
softly ("до 2 устройств") because the panel's limit is not a hard
guarantee (§7.1), and the number of servers is never named because one
subscription mixes protocols and fans out per port (§8.1).
"""

from datetime import datetime

GB = 1024**3

START = (
    '<b>Rillza VPN</b>\n\n'
    'Быстрый VPN без ограничений по трафику: '
    'YouTube, соцсети и любые сервисы работают как обычно.\n\n'
    'Подписка рассчитана на использование до 2 устройств.\n\n'
    'Выберите действие в меню ниже.'
)

TRIAL_OFFER = (
    '🎁 <b>Три дня бесплатно</b>\n\n'
    'Мы дадим полный доступ на 3 дня — без оплаты и без карты.\n'
    'Пробный период доступен один раз.\n\n'
    'Выдать доступ прямо сейчас?'
)

TRIAL_GRANTED = (
    '✅ <b>Готово! Доступ выдан до {until}</b>\n\n'
    'Осталось подключиться — это три шага, справится кто угодно.\n'
    'Нажмите «📖 Как подключить».'
)

TRIAL_ALREADY_USED = (
    'Пробный период уже использован на этом аккаунте.\n\n'
    'Оформить подписку можно в разделе «🛒 Купить подписку».'
)

TRIAL_HAS_SUBSCRIPTION = (
    'У вас уже есть активная подписка — пробный период не нужен.\n'
    'Откройте «🌐 Моя подписка».'
)

PROVISIONING_DELAYED = (
    '⏳ Доступ записан, но сервер пока не ответил.\n\n'
    'Нажмите «🌐 Моя подписка» через минуту — всё появится само. '
    'Деньги и дни при этом не теряются.'
)

NO_SUBSCRIPTION = (
    'У вас пока нет подписки.\n\n'
    'Начните с бесплатного пробного периода или выберите тариф.'
)

SUBSCRIPTION_ACTIVE = (
    '🌐 <b>Ваша подписка</b>\n\nАктивна до <b>{until}</b> ({left})'
)
SUBSCRIPTION_EXPIRED = (
    '🌐 <b>Ваша подписка</b>\n\n'
    '❌ Срок действия закончился {until}.\n\n'
    'Продлите доступ, чтобы снова пользоваться VPN.'
)
SUBSCRIPTION_REVOKED = (
    '🌐 <b>Ваша подписка</b>\n\n'
    '⛔️ Доступ приостановлен. Напишите в поддержку, разберёмся.'
)
SUBSCRIPTION_TRAFFIC = '\nИзрасходовано: <b>{used}</b> (трафик не ограничен)'
SUBSCRIPTION_HINT = (
    '\n\nНажмите «🔗 Открыть подписку» и на открывшейся странице — '
    'кнопку <b>HAPP</b>. Приложение настроится само.'
)

GUIDE = (
    '📖 <b>Как подключиться за три шага</b>\n\n'
    '<b>Шаг 1.</b> Установите приложение <b>Happ</b> — кнопка вашей '
    'системы ниже.\n\n'
    '<b>Шаг 2.</b> Вернитесь сюда и нажмите «🔗 Открыть подписку». '
    'Откроется страница с вашим доступом.\n\n'
    '<b>Шаг 3.</b> На этой странице нажмите кнопку <b>HAPP</b> — '
    'приложение откроется и настроится само. Останется нажать кнопку '
    'подключения внутри Happ.\n\n'
    'Если кнопка не сработала, нажмите «📋 Скопировать ссылку», '
    'откройте Happ, нажмите «+» и выберите «Вставить из буфера».'
)

GUIDE_NEEDS_SUBSCRIPTION = (
    '📖 Инструкция появится вместе с подпиской.\n\n'
    'Начните с бесплатных трёх дней — это займёт одно нажатие.'
)

SUPPORT_PLACEHOLDER = (
    'Раздел поддержки скоро откроется. Пока напишите администратору напрямую.'
)


def format_date(moment: datetime) -> str:
    return moment.strftime('%d.%m.%Y')


def format_left(until: datetime, now: datetime) -> str:
    """Human "сколько осталось" without dragging in a date library."""
    delta = until - now
    if delta.total_seconds() <= 0:
        return 'истекла'
    days = delta.days
    if days >= 1:
        return f'осталось {days} {_plural_days(days)}'
    hours = int(delta.total_seconds() // 3600)
    if hours >= 1:
        return f'осталось {hours} {_plural_hours(hours)}'
    return 'осталось меньше часа'


def format_traffic(used_bytes: int) -> str:
    if used_bytes < GB:
        return f'{used_bytes / (1024 * 1024):.0f} МБ'
    return f'{used_bytes / GB:.1f} ГБ'


def _plural(value: int, one: str, few: str, many: str) -> str:
    if 11 <= value % 100 <= 14:
        return many
    last = value % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _plural_days(value: int) -> str:
    return _plural(value, 'день', 'дня', 'дней')


def _plural_hours(value: int) -> str:
    return _plural(value, 'час', 'часа', 'часов')
