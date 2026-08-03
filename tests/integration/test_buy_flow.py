"""The purchase flow as the user walks it."""

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import keyboards
from app.bot.texts import ru
from app.core.enums import PaymentStatus
from app.core.settings import Settings
from app.integrations.payments import PaymentRegistry
from app.main import build_dispatcher
from app.services.uow import UnitOfWork
from tests.conftest import BASE_ENV
from tests.fake_panel import FakePanel
from tests.fake_payments import FakeProvider
from tests.fake_session import FAKE_TOKEN, RecordingSession
from tests.integration.test_trial_flow import (
    button_texts,
    callback_update,
    edited_texts,
)

USER_ID = 42


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token=FAKE_TOKEN, session=session)


@pytest_asyncio.fixture
async def dispatcher(session_factory, provider, seeded_tariffs):
    settings = Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]
    return build_dispatcher(
        settings,
        session_factory,
        FakePanel(),
        PaymentRegistry({provider.name: provider}),
        storage=MemoryStorage(),
    )


def alerts(session: RecordingSession) -> list[str]:
    return [
        request.text or ''
        for request in session.requests
        if isinstance(request, AnswerCallbackQuery)
    ]


def _last_markup(session: RecordingSession) -> InlineKeyboardMarkup | None:
    markup = None
    for request in session.requests:
        if isinstance(request, SendMessage | EditMessageText):
            markup = request.reply_markup
    return markup


def _button(
    markup: InlineKeyboardMarkup | None, text: str
) -> InlineKeyboardButton:
    for row in markup.inline_keyboard if markup else []:
        for button in row:
            if button.text == text:
                return button
    raise AssertionError(f'no button with text {text!r} on screen')


async def test_buy_asks_for_the_device_count_first(
    dispatcher, bot, session
) -> None:
    await dispatcher.feed_update(bot, callback_update(keyboards.BUY))

    buttons = button_texts(session)
    assert any('2 устройств' in b for b in buttons)
    assert any('4 устройств' in b for b in buttons)
    # The durations are one tap away, not on this screen.
    assert not any('месяц' in b for b in buttons)


async def test_tariff_grid_shows_prices_and_savings(
    dispatcher, bot, session
) -> None:
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}2')
    )

    buttons = button_texts(session)
    # The shortest plan is the reference and carries no badge; longer
    # ones advertise how much cheaper their month is against it.
    assert '1 месяц — 200 ₽' in buttons
    assert '3 месяца — 540 ₽ (выгода 10%)' in buttons
    assert '6 месяцев — 960 ₽ (выгода 20%)' in buttons
    assert any(b.startswith('12 месяцев — 1680 ₽ (выгода 3') for b in buttons)


async def test_the_four_device_grid_keeps_the_same_savings_ladder(
    dispatcher, bot, session
) -> None:
    """A flat multiplier is what makes the badges match between sets.

    A mixed list would compare a two-device month against a
    four-device one and print a nonsense discount.
    """
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}4')
    )

    buttons = button_texts(session)
    assert '1 месяц — 320 ₽' in buttons
    assert '3 месяца — 864 ₽ (выгода 10%)' in buttons
    assert '6 месяцев — 1536 ₽ (выгода 20%)' in buttons
    assert not any('200 ₽' in b for b in buttons)


async def test_a_device_count_not_on_sale_is_rejected(
    dispatcher, bot, session, seeded_tariffs
) -> None:
    """Callback data is client-supplied and need not match a button the
    bot drew. Only 2 and 4 devices are on sale among the seeded
    tariffs, so 6 must be refused rather than opening a tariff screen
    nothing sells.
    """
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}6')
    )

    assert any(ru.PAYMENT_UNKNOWN in alert for alert in alerts(session))
    assert edited_texts(session) == []


async def test_a_withdrawn_four_device_tariff_cannot_be_bought(
    dispatcher, bot, session, session_factory, seeded_tariffs
) -> None:
    """The device screen must not become a second way in.

    Callback data is client-supplied, so taking m1x4 off sale has to
    stop sales of it on both steps, not just hide the button.
    """
    tariff = next(t for t in seeded_tariffs if t.code == 'm1x4')
    async with UnitOfWork(session_factory) as uow:
        await uow.tariffs.set_active(tariff.id, False)
        await uow.commit()
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}4')
    )
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.TARIFF_PREFIX}{tariff.id}')
    )
    await dispatcher.feed_update(
        bot,
        callback_update(f'{keyboards.PROVIDER_PREFIX}{tariff.id}:yoomoney'),
    )

    assert not any('320 ₽' in b for b in button_texts(session))
    async with UnitOfWork(session_factory) as uow:
        assert await uow.payments.list_by_user(USER_ID) == []


async def test_provider_screen_back_button_returns_to_the_tariff_list(
    dispatcher, bot, session, seeded_tariffs
) -> None:
    """`providers()`'s back button used to always target `BUY` — the
    tariff list back in the day, but that callback data now opens the
    device-count screen, so a buyer tapping «Назад» from the provider
    screen would silently skip a step. It must return to the tariff
    list for the device count they actually chose.
    """
    tariff = next(t for t in seeded_tariffs if t.code == 'm1')  # 2 devices
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.TARIFF_PREFIX}{tariff.id}')
    )
    back = _button(_last_markup(session), '↩️ Назад')
    assert back.callback_data == f'{keyboards.DEVICES_PREFIX}2'

    session.requests.clear()
    await dispatcher.feed_update(bot, callback_update(back.callback_data))

    # Feeding the back button's own callback data must bring up the
    # tariff list, not the device-count screen behind it.
    buttons = button_texts(session)
    assert '1 месяц — 200 ₽' in buttons


async def test_invoice_is_created_and_shown(
    dispatcher, bot, session, session_factory, seeded_tariffs
) -> None:
    tariff = seeded_tariffs[0]
    await dispatcher.feed_update(
        bot,
        callback_update(f'{keyboards.PROVIDER_PREFIX}{tariff.id}:yoomoney'),
    )

    text = edited_texts(session)[-1]
    assert 'Счёт на 200 ₽' in text
    assert any('Оплатить 200 ₽' in b for b in button_texts(session))

    async with UnitOfWork(session_factory) as uow:
        payments = await uow.payments.list_by_user(USER_ID)
        assert len(payments) == 1
        assert payments[0].status == PaymentStatus.PENDING


async def test_check_button_reports_unpaid_without_losing_the_invoice(
    dispatcher, bot, session, session_factory, seeded_tariffs
) -> None:
    await dispatcher.feed_update(
        bot,
        callback_update(
            f'{keyboards.PROVIDER_PREFIX}{seeded_tariffs[0].id}:yoomoney'
        ),
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )

    # An alert, and the invoice message is left untouched so the user can
    # still pay it.
    assert any('пока не пришла' in alert for alert in alerts(session))
    assert edited_texts(session) == []


async def test_check_button_delivers_access_once_paid(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    await dispatcher.feed_update(
        bot,
        callback_update(
            f'{keyboards.PROVIDER_PREFIX}{seeded_tariffs[1].id}:yoomoney'
        ),
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )

    assert any('Оплата получена' in text for text in edited_texts(session))
    async with UnitOfWork(session_factory) as uow:
        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        assert subscription.subscription_token is not None


async def test_malformed_payment_id_is_rejected(
    dispatcher, bot, session
) -> None:
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}not-a-uuid')
    )

    assert any('не найден' in alert for alert in alerts(session))


async def test_buy_explains_an_empty_catalog_without_blaming_payments(
    session_factory, provider, bot, session
) -> None:
    """No tariff is on sale at all — a different cause from no payment
    provider being configured, and BUY_NO_PROVIDERS would point at the
    wrong one."""
    settings = Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]
    dispatcher = build_dispatcher(
        settings,
        session_factory,
        FakePanel(),
        PaymentRegistry({provider.name: provider}),
        storage=MemoryStorage(),
    )

    await dispatcher.feed_update(bot, callback_update(keyboards.BUY))

    text = edited_texts(session)[-1]
    assert ru.BUY_NO_TARIFFS in text
    assert 'Оплата' not in text


async def test_purchase_is_hidden_when_no_provider_is_configured(
    session_factory, seeded_tariffs, bot, session
) -> None:
    settings = Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]
    dispatcher = build_dispatcher(
        settings,
        session_factory,
        FakePanel(),
        PaymentRegistry({}),
        storage=MemoryStorage(),
    )

    await dispatcher.feed_update(
        bot,
        callback_update(f'{keyboards.TARIFF_PREFIX}{seeded_tariffs[0].id}'),
    )

    assert any(
        'Оплата временно недоступна' in text for text in edited_texts(session)
    )


async def test_a_payment_cannot_be_finalized_by_another_user(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    """Payment ids travel in callback data — knowing one must not be
    enough to touch someone else's payment."""
    from tests.integration.test_trial_flow import callback_update

    await dispatcher.feed_update(
        bot,
        callback_update(
            f'{keyboards.PROVIDER_PREFIX}{seeded_tariffs[0].id}:yoomoney'
        ),
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    session.requests.clear()

    stranger = 999
    await dispatcher.feed_update(
        bot,
        callback_update(
            f'{keyboards.CHECK_PREFIX}{payment.id}', user_id=stranger
        ),
    )

    # Refused, and nothing was provisioned for anyone.
    assert any('не найден' in alert for alert in alerts(session))
    async with UnitOfWork(session_factory) as uow:
        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PENDING
        assert await uow.subscriptions.get_by_user(stranger) is None
        assert await uow.subscriptions.get_by_user(USER_ID) is None


async def test_a_paid_screen_offers_the_subscription_link(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    """The trial hands over the link; a purchase used to hand over
    nothing but «Главное меню», so the person who just paid had to go
    hunting for what they bought."""
    from tests.integration.test_trial_flow import button_texts, callback_update

    await dispatcher.feed_update(
        bot,
        callback_update(
            f'{keyboards.PROVIDER_PREFIX}{seeded_tariffs[0].id}:yoomoney'
        ),
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )

    buttons = button_texts(session)
    assert any('Открыть подписку' in text for text in buttons)
    assert any('Как подключить' in text for text in buttons)


async def test_a_withdrawn_tariff_cannot_be_bought(
    dispatcher, bot, session, session_factory, seeded_tariffs
) -> None:
    """Callback data is client-supplied, not proof a button was drawn.

    Taking a tariff off sale has to stop sales of it — otherwise a
    retired promo stays purchasable at its old price forever, because
    the row must live on for the payments that reference it.
    """
    from tests.integration.test_trial_flow import callback_update

    tariff = seeded_tariffs[0]
    async with UnitOfWork(session_factory) as uow:
        await uow.tariffs.set_active(tariff.id, False)
        await uow.commit()
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.TARIFF_PREFIX}{tariff.id}')
    )
    await dispatcher.feed_update(
        bot,
        callback_update(f'{keyboards.PROVIDER_PREFIX}{tariff.id}:yoomoney'),
    )

    async with UnitOfWork(session_factory) as uow:
        assert await uow.payments.list_by_user(USER_ID) == []


async def test_a_downgrade_is_warned_about_before_the_tariffs(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    """Days add up, the device count does not: the remaining paid days
    drop to the new number too, and the buyer is told so."""
    four = next(t for t in seeded_tariffs if t.code == 'm1x4')
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.PROVIDER_PREFIX}{four.id}:yoomoney')
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}2')
    )

    text = edited_texts(session)[-1]
    assert 'до 4 устройств' in text
    buttons = button_texts(session)
    assert any('Всё равно продолжить' in b for b in buttons)
    # The tariff list is not on this screen yet.
    assert not any('200 ₽' in b for b in buttons)


async def test_a_confirmed_downgrade_reaches_the_tariffs(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    four = next(t for t in seeded_tariffs if t.code == 'm1x4')
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.PROVIDER_PREFIX}{four.id}:yoomoney')
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}2:ok')
    )

    assert '1 месяц — 200 ₽' in button_texts(session)


async def test_an_upgrade_is_not_warned_about(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    """Only losing devices needs a warning; buying more never does."""
    two = next(t for t in seeded_tariffs if t.code == 'm1')
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.PROVIDER_PREFIX}{two.id}:yoomoney')
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}4')
    )

    assert '1 месяц — 320 ₽' in button_texts(session)


async def test_an_archived_tariff_cannot_be_bought(
    dispatcher, bot, session, session_factory, seeded_tariffs
) -> None:
    from tests.integration.test_trial_flow import callback_update

    tariff = seeded_tariffs[1]
    async with UnitOfWork(session_factory) as uow:
        stored = await uow.tariffs.get(tariff.id)
        stored.is_archived = True
        await uow.commit()
    session.requests.clear()

    await dispatcher.feed_update(
        bot,
        callback_update(f'{keyboards.PROVIDER_PREFIX}{tariff.id}:yoomoney'),
    )

    async with UnitOfWork(session_factory) as uow:
        assert await uow.payments.list_by_user(USER_ID) == []
