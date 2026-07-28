"""Read-only check of the payment credentials.

Asks each configured provider who its token belongs to. Creates no
invoice and moves no money, so it is safe to run against production.

    uv run python -m scripts.check_payments

In a deployed container:

    docker compose exec bot python -m scripts.check_payments

A provider without credentials is not an error: the purchase screen
simply does not offer it. Both missing is, because then nothing can be
sold.
"""

import asyncio
import sys

from app.core.settings import get_settings
from app.integrations.payments import PaymentError, PaymentRegistry

OK = '  OK  '
FAIL = ' FAIL '
SKIP = ' SKIP '


async def main() -> int:
    settings = get_settings()
    registry = PaymentRegistry.from_settings(settings)
    available = registry.available()

    if not available:
        print(
            f'[{FAIL}] no payment provider is configured. Set '
            'YOOMONEY_ACCESS_TOKEN\n'
            '         or CRYPTOBOT_TOKEN — until then the purchase screen '
            'has nothing\n'
            '         to offer and only the trial works.'
        )
        return 1

    failures = 0
    try:
        for name in available:
            provider = registry.get(name)
            if provider is None:  # pragma: no cover - available() said yes
                continue
            try:
                print(f'[{OK}] {name}: {await provider.describe_account()}')
            except PaymentError as error:
                print(f'[{FAIL}] {name}: {error}')
                failures += 1
    finally:
        await registry.close()

    for name in ('yoomoney', 'cryptobot'):
        if name not in available:
            print(f'[{SKIP}] {name}: no token, this method stays hidden')

    if 'yoomoney' in available and not settings.bot_url:
        print(
            f'[{FAIL}] BOT_URL is empty, so YooMoney has nowhere to return '
            'the payer\n'
            '         after paying. Set it to your bot link, e.g. '
            'https://t.me/your_bot.'
        )
        failures += 1

    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
