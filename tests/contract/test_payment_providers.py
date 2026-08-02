"""Contract tests for the payment providers, against mocked HTTP.

These pin the behaviours the previous stack got wrong: the label must be
filtered server-side, an outgoing operation must never count as payment,
amounts must carry kopeks, and CryptoBot must be priced in rubles.
"""

import re
import uuid

import pytest
import pytest_asyncio
from aioresponses import aioresponses

from app.integrations.payments import (
    CryptoBotProvider,
    PaymentError,
    ProviderStatus,
    YooMoneyProvider,
)

PAYMENT_ID = uuid.UUID('11111111-2222-3333-4444-555555555555')
HISTORY = 'https://yoomoney.ru/api/operation-history'
# aiohttp appends the params to the URL, so the mock matches a pattern.
QUICKPAY = re.compile(r'^https://yoomoney\.ru/quickpay/confirm\.xml.*$')
CRYPTO = 'https://pay.crypt.bot/api'


@pytest.fixture
def mocked():
    with aioresponses() as mock:
        yield mock


def last_request(mocked):
    return list(mocked.requests.values())[-1][-1]


def requested_paths(mocked) -> list[str]:
    """Every path hit, in order. The key is (method, URL)."""
    return [str(key[1].path) for key in mocked.requests]


@pytest_asyncio.fixture
async def yoomoney(make_settings):
    settings = make_settings(
        yoomoney_access_token='ym-token',
        yoomoney_wallet='4100111111111',
        bot_url='https://t.me/rillza_bot',
    )
    provider = YooMoneyProvider(settings)
    yield provider
    await provider.close()


@pytest_asyncio.fixture
async def cryptobot(make_settings):
    provider = CryptoBotProvider(make_settings(cryptobot_token='cb-token'))
    yield provider
    await provider.close()


class TestYooMoney:
    async def test_missing_token_is_refused_at_construction(
        self, settings
    ) -> None:
        with pytest.raises(PaymentError):
            YooMoneyProvider(settings)

    async def test_invoice_carries_label_and_kopeks(
        self, yoomoney, mocked
    ) -> None:
        mocked.post(QUICKPAY, status=200, body='<html/>')

        invoice = await yoomoney.create_invoice(
            PAYMENT_ID, 54_050, 'Rillza VPN — 3 месяца', 30
        )

        params = last_request(mocked).kwargs['params']
        assert params['label'] == str(PAYMENT_ID)
        # Kopeks survive: aiomoney only accepted whole rubles.
        assert params['sum'] == '540.50'
        assert params['receiver'] == '4100111111111'
        assert params['successURL'] == 'https://t.me/rillza_bot'
        assert invoice.url

    async def test_check_filters_by_label_server_side(
        self, yoomoney, mocked
    ) -> None:
        """The fix for the bug that loses payments under load.

        aiomoney fetched the unfiltered history and searched the first
        page in Python; a busy wallet pushes the payment off that page
        and it is never found.
        """
        mocked.post(HISTORY, payload={'operations': []})

        await yoomoney.check_payment(PAYMENT_ID)

        assert last_request(mocked).kwargs['data']['label'] == str(PAYMENT_ID)

    async def test_incoming_success_is_paid(self, yoomoney, mocked) -> None:
        mocked.post(
            HISTORY,
            payload={
                'operations': [
                    {
                        'label': str(PAYMENT_ID),
                        'direction': 'in',
                        'status': 'success',
                        'amount': 198.0,
                    }
                ]
            },
        )

        check = await yoomoney.check_payment(PAYMENT_ID)

        assert check.status is ProviderStatus.PAID
        # Net of the sender's fee — recorded, never compared to the price.
        assert check.paid_amount_kopeks == 19_800
        assert check.paid_currency == 'RUB'

    @pytest.mark.parametrize(
        'operation',
        [
            {'direction': 'out', 'status': 'success'},
            {'direction': 'in', 'status': 'in_progress'},
            {'direction': 'in', 'status': 'refused'},
        ],
    )
    async def test_non_incoming_or_unfinished_is_not_paid(
        self, yoomoney, mocked, operation
    ) -> None:
        """An outgoing operation with our label must not count as payment."""
        mocked.post(
            HISTORY,
            payload={
                'operations': [
                    {'label': str(PAYMENT_ID), 'amount': 200.0, **operation}
                ]
            },
        )

        check = await yoomoney.check_payment(PAYMENT_ID)

        assert check.status is ProviderStatus.PENDING

    async def test_unreachable_api_raises_payment_error(
        self, yoomoney, mocked
    ) -> None:
        import aiohttp

        mocked.post(HISTORY, exception=aiohttp.ClientError('boom'))

        with pytest.raises(PaymentError):
            await yoomoney.check_payment(PAYMENT_ID)

    async def test_wallet_is_read_from_api_when_unset(
        self, make_settings, mocked
    ) -> None:
        provider = YooMoneyProvider(
            make_settings(yoomoney_access_token='ym-token')
        )
        mocked.post(
            'https://yoomoney.ru/api/account-info',
            payload={'account': '4100999'},
        )
        mocked.post(QUICKPAY, status=200, body='<html/>')

        await provider.create_invoice(PAYMENT_ID, 20_000, 'x', 30)

        assert last_request(mocked).kwargs['params']['receiver'] == '4100999'
        await provider.close()

    async def test_describe_account_reads_but_never_writes(
        self, yoomoney, mocked
    ) -> None:
        """The credentials check must not be able to bill anyone."""
        mocked.post(
            'https://yoomoney.ru/api/account-info',
            payload={
                'account': '4100111111111',
                'balance': 250.5,
                'currency': '643',
            },
        )
        mocked.post(HISTORY, payload={'operations': []})

        described = await yoomoney.describe_account()

        assert '4100111111111' in described
        assert '250.5' in described
        # Only the two read endpoints, no quickpay.
        assert requested_paths(mocked) == [
            '/api/account-info',
            '/api/operation-history',
        ]

    async def test_describe_account_fails_without_the_history_scope(
        self, yoomoney, mocked
    ) -> None:
        """The failure this check exists for.

        A token granted account-info but not operation-history issues
        invoices perfectly and confirms none of them: money arrives and
        nobody gets access. account-info alone cannot see that.
        """
        mocked.post(
            'https://yoomoney.ru/api/account-info',
            payload={'account': '4100111111111'},
        )
        mocked.post(HISTORY, status=403, payload={'error': 'scope_error'})

        with pytest.raises(PaymentError) as error:
            await yoomoney.describe_account()

        assert 'operation-history' in str(error.value)

    async def test_scope_probe_cannot_match_a_real_payment(
        self, yoomoney, mocked
    ) -> None:
        """The probe reads nobody's operations: its label is unusable."""
        mocked.post(
            'https://yoomoney.ru/api/account-info',
            payload={'account': '4100111111111'},
        )
        mocked.post(HISTORY, payload={'operations': []})

        await yoomoney.describe_account()

        sent = last_request(mocked).kwargs['data']
        assert sent['label'] == '00000000-0000-0000-0000-000000000000'
        assert uuid.UUID(sent['label']).int == 0

    async def test_non_json_body_is_a_payment_error(
        self, yoomoney, mocked
    ) -> None:
        """A proxy in front of the API answers HTML with status 200.

        Callers handle PaymentError and nothing else, so a JSON decode
        error escaping here would crash a poll cycle instead of showing
        "платёжная система не отвечает".
        """
        mocked.post(
            'https://yoomoney.ru/api/account-info',
            status=200,
            body='<html>blocked</html>',
        )

        with pytest.raises(PaymentError):
            await yoomoney.describe_account()

    async def test_describe_account_rejects_an_empty_answer(
        self, yoomoney, mocked
    ) -> None:
        """A revoked token answers 200 with nothing useful."""
        mocked.post('https://yoomoney.ru/api/account-info', payload={})

        with pytest.raises(PaymentError):
            await yoomoney.describe_account()


class TestCryptoBot:
    async def test_invoice_is_priced_in_rubles(
        self, cryptobot, mocked
    ) -> None:
        """fiat=RUB keeps one price column and no exchange-rate code."""
        mocked.post(
            f'{CRYPTO}/createInvoice',
            payload={
                'ok': True,
                'result': {
                    'invoice_id': 777,
                    'bot_invoice_url': 'https://t.me/CryptoBot?start=x',
                    'status': 'active',
                },
            },
        )

        invoice = await cryptobot.create_invoice(
            PAYMENT_ID, 96_000, 'Rillza VPN — 6 месяцев', 30
        )

        body = last_request(mocked).kwargs['json']
        assert body['currency_type'] == 'fiat'
        assert body['fiat'] == 'RUB'
        assert body['amount'] == '960.00'
        assert body['payload'] == str(PAYMENT_ID)
        assert body['expires_in'] == 30 * 60
        assert invoice.provider_invoice_id == '777'
        assert invoice.url.startswith('https://t.me/CryptoBot')

    async def test_check_looks_the_invoice_up_by_id(
        self, cryptobot, mocked
    ) -> None:
        """Scanning the recent window would lose invoices on a busy day."""
        mocked.post(
            f'{CRYPTO}/getInvoices',
            payload={
                'ok': True,
                'result': {
                    'items': [
                        {
                            'invoice_id': 777,
                            'payload': str(PAYMENT_ID),
                            'status': 'paid',
                            'amount': '960.00',
                            'fiat': 'RUB',
                        }
                    ]
                },
            },
        )

        check = await cryptobot.check_payment(PAYMENT_ID, '777')

        assert last_request(mocked).kwargs['json']['invoice_ids'] == '777'
        assert check.status is ProviderStatus.PAID
        assert check.paid_amount_kopeks == 96_000

    @pytest.mark.parametrize(
        'status, expected',
        [
            ('active', ProviderStatus.PENDING),
            ('expired', ProviderStatus.EXPIRED),
        ],
    )
    async def test_statuses_are_mapped(
        self, cryptobot, mocked, status, expected
    ) -> None:
        mocked.post(
            f'{CRYPTO}/getInvoices',
            payload={
                'ok': True,
                'result': {
                    'items': [{'payload': str(PAYMENT_ID), 'status': status}]
                },
            },
        )

        check = await cryptobot.check_payment(PAYMENT_ID, '777')

        assert check.status is expected

    async def test_api_error_is_raised(self, cryptobot, mocked) -> None:
        mocked.post(
            f'{CRYPTO}/createInvoice',
            payload={'ok': False, 'error': {'code': 400}},
        )

        with pytest.raises(PaymentError):
            await cryptobot.create_invoice(PAYMENT_ID, 20_000, 'x', 30)

    async def test_describe_account_names_the_app(
        self, cryptobot, mocked
    ) -> None:
        mocked.post(
            f'{CRYPTO}/getMe',
            payload={
                'ok': True,
                'result': {
                    'app_id': 42,
                    'name': 'Rillza',
                    'payment_processing_bot_username': 'CryptoBot',
                },
            },
        )

        described = await cryptobot.describe_account()

        assert 'Rillza' in described
        assert 'CryptoBot' in described
        assert requested_paths(mocked) == ['/api/getMe']

    async def test_non_json_body_is_a_payment_error(
        self, cryptobot, mocked
    ) -> None:
        """CryptoBot parses before checking the status, so any error
        page reaches the JSON decoder first."""
        mocked.post(
            f'{CRYPTO}/getMe', status=403, body='<html>forbidden</html>'
        )

        with pytest.raises(PaymentError):
            await cryptobot.describe_account()

    async def test_describe_account_surfaces_a_bad_token(
        self, cryptobot, mocked
    ) -> None:
        mocked.post(
            f'{CRYPTO}/getMe', payload={'ok': False, 'error': {'code': 401}}
        )

        with pytest.raises(PaymentError):
            await cryptobot.describe_account()

    async def test_missing_token_is_refused(self, settings) -> None:
        with pytest.raises(PaymentError):
            CryptoBotProvider(settings)


class TestCryptoBotInvoiceId:
    async def test_an_invoice_without_an_id_is_refused(
        self, mocked, make_settings
    ) -> None:
        """Stored as the string 'None' it addresses nothing: the lookup
        never matches, so the payment sits pending until it expires, and
        a second one collides on the unique index."""
        from app.integrations.payments.cryptobot import CryptoBotProvider

        provider = CryptoBotProvider(make_settings(cryptobot_token='cb'))
        mocked.post(
            'https://pay.crypt.bot/api/createInvoice',
            payload={'ok': True, 'result': {'bot_invoice_url': 'https://x'}},
        )

        with pytest.raises(PaymentError):
            await provider.create_invoice(
                uuid.uuid4(),
                amount_kopeks=20_000,
                description='x',
                ttl_minutes=30,
            )
        await provider.close()
