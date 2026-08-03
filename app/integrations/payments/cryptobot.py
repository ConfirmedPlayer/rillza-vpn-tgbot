"""CryptoBot (@CryptoBot) invoices priced in rubles.

Invoices are created with ``currency_type=fiat`` and ``fiat=RUB``, so
CryptoBot performs the conversion and the bot keeps exactly one price
column and one currency in its revenue figures — no exchange-rate code
and no drift between a ruble price and a crypto price.
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

API_URL = 'https://pay.crypt.bot/api'
TIMEOUT = aiohttp.ClientTimeout(total=20)

STATUS_MAP = {
    'active': ProviderStatus.PENDING,
    'paid': ProviderStatus.PAID,
    'expired': ProviderStatus.EXPIRED,
}


class CryptoBotProvider:
    name = 'cryptobot'

    def __init__(
        self, settings: Settings, session: aiohttp.ClientSession | None = None
    ) -> None:
        if settings.cryptobot_token is None:
            raise PaymentError('CRYPTOBOT_TOKEN is not configured')
        self._token = settings.cryptobot_token.get_secret_value()
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

    async def _api(self, method: str, payload: dict[str, Any]) -> Any:
        session = self._ensure_session()
        try:
            async with session.post(
                f'{API_URL}/{method}',
                json=payload,
                headers={'Crypto-Pay-API-Token': self._token},
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except ValueError as error:
                    # A proxy, a WAF or a maintenance page answers HTML.
                    # Callers only handle PaymentError, so letting a
                    # JSONDecodeError out turns a provider hiccup into a
                    # crashed poll or a bare "что-то пошло не так".
                    raise PaymentError(
                        f'CryptoBot {method} answered HTTP {response.status} '
                        'with a non-JSON body'
                    ) from error
        except aiohttp.ClientError as error:
            raise PaymentError(f'CryptoBot unreachable: {error!r}') from error
        except TimeoutError as error:
            raise PaymentError(f'CryptoBot timed out: {error!r}') from error

        if not isinstance(body, dict) or not body.get('ok'):
            error = body.get('error') if isinstance(body, dict) else body
            raise PaymentError(f'CryptoBot {method} failed: {error}')
        return body.get('result')

    async def describe_account(self) -> str:
        """The app the token belongs to. Creates nothing."""
        result = await self._api('getMe', {})
        if not isinstance(result, dict):
            raise PaymentError('CryptoBot getMe returned no app')
        name = result.get('name') or result.get('app_id') or '?'
        bot = result.get('payment_processing_bot_username')
        return f'app {name}' + (f' via @{bot}' if bot else '')

    async def create_invoice(
        self,
        payment_id: UUID,
        amount_kopeks: int,
        description: str,
        ttl_minutes: int,
    ) -> Invoice:
        result = await self._api(
            'createInvoice',
            {
                'currency_type': 'fiat',
                'fiat': 'RUB',
                'amount': f'{amount_kopeks / 100:.2f}',
                # Our payment id travels back on every lookup.
                'payload': str(payment_id),
                'description': description[:1024],
                'expires_in': ttl_minutes * 60,
                'allow_comments': False,
                'allow_anonymous': True,
            },
        )
        url = result.get('bot_invoice_url') or result.get('pay_url')
        if not url:
            raise PaymentError('CryptoBot returned an invoice without a URL')
        invoice_id = result.get('invoice_id')
        if invoice_id is None:
            # Stored as the string 'None' it addresses nothing: the
            # lookup by invoice_ids never matches, so the payment stays
            # pending until it expires, and a second one collides on
            # uq_payments_provider_invoice_id and kills create_invoice.
            raise PaymentError('CryptoBot returned an invoice without an id')
        return Invoice(url=str(url), provider_invoice_id=str(invoice_id))

    async def check_payment(
        self, payment_id: UUID, provider_invoice_id: str | None = None
    ) -> PaymentCheck:
        """Look the invoice up by id when we have one.

        Falling back to the recent-invoice window would repeat the bug we
        removed from the YooMoney path: a busy day pushes the invoice off
        the page and the payment is never found.
        """
        query: dict[str, Any] = (
            {'invoice_ids': provider_invoice_id}
            if provider_invoice_id
            else {'count': 100}
        )
        result = await self._api('getInvoices', query)
        items = result.get('items', []) if isinstance(result, dict) else []

        for invoice in items:
            if invoice.get('payload') != str(payment_id):
                continue
            status = STATUS_MAP.get(
                str(invoice.get('status')), ProviderStatus.PENDING
            )
            if status is not ProviderStatus.PAID:
                return PaymentCheck(status)
            return PaymentCheck(
                ProviderStatus.PAID,
                paid_amount_kopeks=_settled_kopeks(invoice),
                paid_currency=str(invoice.get('fiat') or 'RUB'),
            )
        # Not in the recent window: our own TTL decides when to give up.
        return PaymentCheck(ProviderStatus.PENDING)


def _settled_kopeks(invoice: dict[str, Any]) -> int | None:
    """What actually arrived, in the currency the invoice was priced in.

    A fiat invoice is paid in crypto: ``paid_amount`` is denominated in
    ``paid_asset``, and ``paid_fiat_rate`` is that asset's price in
    ``fiat``, so their product is the rouble value the buyer settled.
    ``amount`` is only what was asked for — the rate moves between
    issuing an invoice and paying it, which is the whole reason this
    column is recorded rather than assumed.

    This used to read ``paid_fiat_rate_amount``. There is no such field
    in the Crypto Pay API, so the lookup always missed and the ``or``
    fell through to the sticker price for every crypto payment ever
    made — a column that reads as an observation while holding a
    constant.

    CryptoBot's own fee (``fee_amount``, in ``paid_asset``) is *not*
    subtracted. No crypto purchase has ever gone through this bot, so
    whether ``paid_amount`` is already net of it is unverified, and a
    computed guess in a column the operator reads as observed is worse
    than a gross figure that says what it is. Worth settling against
    the wallet on the first real one.
    """
    paid = _to_float(invoice.get('paid_amount'))
    rate = _to_float(invoice.get('paid_fiat_rate'))
    if paid is not None and rate is not None:
        return round(paid * rate * 100)
    return _to_kopeks(invoice.get('amount'))


def _to_float(amount: Any) -> float | None:
    if amount is None:
        return None
    try:
        return float(amount)
    except (TypeError, ValueError):
        return None


def _to_kopeks(amount: Any) -> int | None:
    value = _to_float(amount)
    return None if value is None else round(value * 100)
