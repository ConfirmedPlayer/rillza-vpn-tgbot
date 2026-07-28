"""YooMoney p2p wallet payments.

Same API the previous bot used through ``aiomoney`` — transfers to a
personal wallet, so no merchant account is required — but written here
because that library has two defects that lose money:

* it requests the operation history with **no** ``label`` parameter and
  filters in Python, while the API returns only the first page (30
  operations by default). Under any real flow a payment scrolls off that
  page and is never found. We filter server-side.
* it checks ``status == 'success'`` without checking ``direction``, so an
  outgoing operation carrying the same label would count as payment.

Amounts are sent with kopeks; ``aiomoney`` only accepted whole rubles.
"""

from typing import Any
from uuid import UUID

import aiohttp

from app.core.settings import Settings
from app.integrations.payments.base import (
    Invoice,
    PaymentCheck,
    PaymentError,
    ProviderStatus,
)

API_HOST = 'https://yoomoney.ru'
QUICKPAY_URL = f'{API_HOST}/quickpay/confirm.xml'
ACCOUNT_INFO_URL = f'{API_HOST}/api/account-info'
OPERATION_HISTORY_URL = f'{API_HOST}/api/operation-history'
TIMEOUT = aiohttp.ClientTimeout(total=20)


class YooMoneyProvider:
    name = 'yoomoney'

    def __init__(
        self, settings: Settings, session: aiohttp.ClientSession | None = None
    ) -> None:
        if settings.yoomoney_access_token is None:
            raise PaymentError('YOOMONEY_ACCESS_TOKEN is not configured')
        self._token = settings.yoomoney_access_token.get_secret_value()
        self._wallet = settings.yoomoney_wallet
        self._payment_type = settings.yoomoney_payment_type
        self._success_url = settings.bot_url
        self._session = session
        self._owns_session = session is None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _api(self, url: str, data: dict[str, Any]) -> Any:
        session = self._ensure_session()
        try:
            async with session.post(
                url,
                data=data,
                headers={'Authorization': f'Bearer {self._token}'},
            ) as response:
                if response.status != 200:
                    raise PaymentError(
                        f'YooMoney {url} answered HTTP {response.status}'
                    )
                try:
                    return await response.json(content_type=None)
                except ValueError as error:
                    # 200 with an HTML body: a captive portal or a proxy
                    # standing in front of the API. PaymentError is the
                    # only failure callers know how to handle.
                    raise PaymentError(
                        f'YooMoney {url} answered 200 with a non-JSON body'
                    ) from error
        except aiohttp.ClientError as error:
            raise PaymentError(f'YooMoney unreachable: {error!r}') from error
        except TimeoutError as error:
            raise PaymentError(f'YooMoney timed out: {error!r}') from error

    async def wallet_number(self) -> str:
        """The receiving wallet, from config or from the API."""
        if self._wallet:
            return self._wallet
        payload = await self._api(ACCOUNT_INFO_URL, {})
        account = payload.get('account') if isinstance(payload, dict) else None
        if not account:
            raise PaymentError('YooMoney account-info returned no account')
        self._wallet = str(account)
        return self._wallet

    async def describe_account(self) -> str:
        """Wallet and balance — proves the token carries both scopes.

        ``account-info`` needs the scope of the same name; the balance it
        returns is also the only read that tells a live token from one
        that was revoked in the YooMoney cabinet.
        """
        payload = await self._api(ACCOUNT_INFO_URL, {})
        if not isinstance(payload, dict) or not payload.get('account'):
            raise PaymentError('YooMoney account-info returned no account')
        balance = payload.get('balance')
        currency = payload.get('currency', '')
        wallet = str(payload['account'])
        if balance is None:
            return f'wallet {wallet}'
        return f'wallet {wallet}, balance {balance} {currency}'.rstrip()

    async def create_invoice(
        self,
        payment_id: UUID,
        amount_kopeks: int,
        description: str,
        ttl_minutes: int,
    ) -> Invoice:
        """Build a quickpay form and return the page the user opens.

        The label is our payment id, which is what makes the later check
        an exact lookup rather than a scan.
        """
        params = {
            'receiver': await self.wallet_number(),
            'quickpay-form': 'button',
            'paymentType': self._payment_type,
            'sum': f'{amount_kopeks / 100:.2f}',
            'label': str(payment_id),
            'targets': description,
        }
        if self._success_url:
            params['successURL'] = self._success_url

        session = self._ensure_session()
        try:
            async with session.post(
                QUICKPAY_URL, params=params, allow_redirects=True
            ) as response:
                if response.status >= 400:
                    raise PaymentError(
                        f'YooMoney quickpay answered HTTP {response.status}'
                    )
                return Invoice(url=str(response.url))
        except aiohttp.ClientError as error:
            raise PaymentError(f'YooMoney unreachable: {error!r}') from error
        except TimeoutError as error:
            raise PaymentError(f'YooMoney timed out: {error!r}') from error

    async def check_payment(
        self, payment_id: UUID, provider_invoice_id: str | None = None
    ) -> PaymentCheck:
        """Look the payment up by label, server-side.

        YooMoney has no notion of an expired invoice — a quickpay link
        stays payable — so an unseen payment is simply still pending and
        our own TTL decides when to stop asking.
        """
        payload = await self._api(
            OPERATION_HISTORY_URL,
            {'label': str(payment_id), 'type': 'deposition'},
        )
        operations = (
            payload.get('operations') if isinstance(payload, dict) else None
        )
        if not operations:
            return PaymentCheck(ProviderStatus.PENDING)

        for operation in operations:
            if operation.get('label') != str(payment_id):
                continue
            if operation.get('direction') != 'in':
                continue
            if operation.get('status') != 'success':
                continue
            return PaymentCheck(
                ProviderStatus.PAID,
                paid_amount_kopeks=_to_kopeks(operation.get('amount')),
                paid_currency='RUB',
            )
        return PaymentCheck(ProviderStatus.PENDING)


def _to_kopeks(amount: Any) -> int | None:
    if amount is None:
        return None
    try:
        return round(float(amount) * 100)
    except (TypeError, ValueError):
        return None
