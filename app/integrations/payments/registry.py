"""Which payment providers this deployment actually offers.

A provider without credentials is simply absent, so the purchase screen
shows only what can really be paid.
"""

from app.core.enums import PaymentProvider as ProviderName
from app.core.settings import Settings
from app.integrations.payments.base import PaymentProvider
from app.integrations.payments.cryptobot import CryptoBotProvider
from app.integrations.payments.yoomoney import YooMoneyProvider

TITLES = {
    ProviderName.YOOMONEY: '💳 Банковская карта',
    ProviderName.CRYPTOBOT: '🪙 Криптовалюта',
}


class PaymentRegistry:
    def __init__(self, providers: dict[str, PaymentProvider]) -> None:
        self._providers = providers

    @classmethod
    def from_settings(cls, settings: Settings) -> 'PaymentRegistry':
        providers: dict[str, PaymentProvider] = {}
        if settings.yoomoney_access_token is not None:
            providers[ProviderName.YOOMONEY] = YooMoneyProvider(settings)
        if settings.cryptobot_token is not None:
            providers[ProviderName.CRYPTOBOT] = CryptoBotProvider(settings)
        return cls(providers)

    def get(self, name: str) -> PaymentProvider | None:
        return self._providers.get(name)

    def available(self) -> list[str]:
        return list(self._providers)

    @staticmethod
    def title(name: str) -> str:
        return TITLES.get(ProviderName(name), name)

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
