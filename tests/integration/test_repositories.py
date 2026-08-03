"""Repository behaviour against a real PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import (
    PaymentProvider,
    PaymentStatus,
    SubscriptionOrigin,
    SubscriptionStatus,
)
from app.db.models import Payment, Subscription, User
from app.services.uow import UnitOfWork

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


async def make_user(uow: UnitOfWork, telegram_id: int = 42) -> User:
    user = await uow.users.upsert(
        telegram_id, username='ivan', first_name='Иван'
    )
    await uow.commit()
    return user


def make_subscription(user_id: int, **overrides) -> Subscription:
    values = {
        'user_id': user_id,
        'status': SubscriptionStatus.ACTIVE,
        'origin': SubscriptionOrigin.TRIAL,
        'expires_at': NOW + timedelta(days=3),
        'panel_user_id': str(user_id),
    }
    values.update(overrides)
    return Subscription(**values)


def make_payment(user_id: int, tariff_id: int, **overrides) -> Payment:
    values = {
        'user_id': user_id,
        'tariff_id': tariff_id,
        'provider': PaymentProvider.YOOMONEY,
        'status': PaymentStatus.PENDING,
        'amount_kopeks': 20_000,
        'invoice_expires_at': NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    return Payment(**values)


class TestUsers:
    async def test_upsert_creates_then_updates(self, uow: UnitOfWork) -> None:
        await uow.users.upsert(1, username='old', first_name='Старое')
        await uow.commit()

        user = await uow.users.upsert(1, username='new', first_name='Новое')
        await uow.commit()

        assert user.id == 1
        assert user.username == 'new'
        assert await uow.users.count() == 1

    async def test_upsert_keeps_the_trial_latch(self, uow: UnitOfWork) -> None:
        """A profile refresh must not hand out a second free trial."""
        await uow.users.upsert(1)
        await uow.users.mark_trial_used(1, NOW)
        await uow.commit()

        await uow.users.upsert(1, username='renamed')
        await uow.commit()

        user = await uow.users.get(1)
        assert user is not None
        assert user.trial_used_at is not None

    async def test_upsert_clears_the_blocked_flag(
        self, uow: UnitOfWork
    ) -> None:
        """Unlike the trial latch, this one describes reachability.

        A blocked user cannot send anything, so an incoming update is
        proof the block is gone. Keeping the flag set left everyone who
        blocked the bot once out of every broadcast for good.
        """
        await uow.users.upsert(1)
        await uow.users.set_bot_blocked(1, True)
        await uow.commit()

        await uow.users.upsert(1, username='renamed')
        await uow.commit()

        user = await uow.users.get(1)
        assert user is not None
        assert user.is_bot_blocked is False

    async def test_trial_latch_grants_once(self, uow: UnitOfWork) -> None:
        await make_user(uow, 1)

        first = await uow.users.mark_trial_used(1, NOW)
        second = await uow.users.mark_trial_used(1, NOW + timedelta(days=1))
        await uow.commit()

        assert first is not None
        assert second is None
        assert first.trial_used_at == NOW

    async def test_broadcast_targets_skip_blocked_and_paginate(
        self, uow: UnitOfWork
    ) -> None:
        for telegram_id in (1, 2, 3, 4):
            await uow.users.upsert(telegram_id)
        await uow.users.set_bot_blocked(3, True)
        await uow.commit()

        first_page = await uow.users.iter_broadcast_targets(limit=2)
        second_page = await uow.users.iter_broadcast_targets(
            after_id=first_page[-1].id, limit=2
        )

        assert [user.id for user in first_page] == [1, 2]
        assert [user.id for user in second_page] == [4]


class TestTariffs:
    async def test_seed_matches_agreed_prices(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        tariffs = await uow.tariffs.list_active()

        assert [(t.code, t.price_kopeks) for t in tariffs] == [
            ('m1', 20_000),
            ('m3', 54_000),
            ('m6', 96_000),
            ('m12', 168_000),
            ('m1x4', 32_000),
            ('m3x4', 86_400),
            ('m6x4', 153_600),
            ('m12x4', 268_800),
        ]

    async def test_monthly_price_shows_the_discount(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        by_code = {t.code: t for t in await uow.tariffs.list_active()}

        assert by_code['m1'].monthly_price_kopeks == 20_000
        assert by_code['m3'].monthly_price_kopeks == 18_000
        assert by_code['m6'].monthly_price_kopeks == 16_000
        assert by_code['m1'].price_rubles == 200

    async def test_inactive_and_archived_are_hidden(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        tariffs = {t.code: t for t in await uow.tariffs.list_all()}
        tariffs['m3'].is_active = False
        tariffs['m6'].is_archived = True
        await uow.commit()

        active = [t.code for t in await uow.tariffs.list_active()]
        listed = [t.code for t in await uow.tariffs.list_all()]

        assert active == ['m1', 'm12', 'm1x4', 'm3x4', 'm6x4', 'm12x4']
        assert 'm6' not in listed

    async def test_code_is_unique(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        from app.db.models import Tariff

        uow.session.add(
            Tariff(
                code='m1', title_ru='Дубль', duration_days=30, price_kopeks=1
            )
        )
        with pytest.raises(IntegrityError):
            await uow.session.flush()


class TestSubscriptions:
    async def test_one_subscription_per_user(self, uow: UnitOfWork) -> None:
        await make_user(uow, 1)
        await uow.subscriptions.add(make_subscription(1))
        await uow.commit()

        with pytest.raises(IntegrityError):
            await uow.subscriptions.add(make_subscription(1))

    async def test_status_check_constraint(self, uow: UnitOfWork) -> None:
        await make_user(uow, 1)

        with pytest.raises(IntegrityError):
            await uow.subscriptions.add(make_subscription(1, status='bogus'))

    async def test_expiring_window_is_half_open(self, uow: UnitOfWork) -> None:
        for telegram_id, days in ((1, 1), (2, 3), (3, 10)):
            await make_user(uow, telegram_id)
            await uow.subscriptions.add(
                make_subscription(
                    telegram_id, expires_at=NOW + timedelta(days=days)
                )
            )
        await uow.commit()

        soon = await uow.subscriptions.list_expiring_between(
            NOW, NOW + timedelta(days=4)
        )

        assert {s.user_id for s in soon} == {1, 2}

    async def test_mark_expired_is_idempotent(self, uow: UnitOfWork) -> None:
        await make_user(uow, 1)
        subscription = make_subscription(
            1, expires_at=NOW - timedelta(minutes=1), notified_stage='1d'
        )
        await uow.subscriptions.add(subscription)
        await uow.commit()

        first = await uow.subscriptions.mark_expired(subscription.id, NOW)
        second = await uow.subscriptions.mark_expired(subscription.id, NOW)
        await uow.commit()

        assert first is not None
        assert second is None
        assert first.status == SubscriptionStatus.EXPIRED
        # Cleared so a renewal starts its reminder cycle fresh.
        assert first.notified_stage is None

    async def test_deleting_user_cascades_to_subscription(
        self, uow: UnitOfWork
    ) -> None:
        user = await make_user(uow, 1)
        await uow.subscriptions.add(make_subscription(1))
        await uow.commit()

        await uow.session.delete(user)
        await uow.commit()

        assert await uow.subscriptions.get_by_user(1) is None

    async def test_deleting_user_with_payments_is_refused(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        """Money records must never disappear with the user."""
        user = await make_user(uow, 1)
        await uow.payments.add(make_payment(1, seeded_tariffs[0].id))
        await uow.commit()

        await uow.session.delete(user)
        with pytest.raises(IntegrityError):
            await uow.commit()


class TestPayments:
    async def test_mark_paid_is_compare_and_set(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        await make_user(uow, 1)
        payment = await uow.payments.add(make_payment(1, seeded_tariffs[0].id))
        await uow.commit()
        target = NOW + timedelta(days=30)

        first = await uow.payments.mark_paid(payment.id, NOW, target)
        second = await uow.payments.mark_paid(
            payment.id, NOW + timedelta(minutes=5), NOW + timedelta(days=60)
        )
        await uow.commit()

        assert first is not None
        assert second is None
        assert first.status == PaymentStatus.PAID
        # The frozen target must survive the second attempt: a retry that
        # recomputed it would hand the user 60 days for one payment.
        assert first.target_expires_at == target
        # And the object the rest of the app reads must agree with the row.
        reread = await uow.payments.get(payment.id)
        assert reread is not None
        assert reread.target_expires_at == target

    async def test_provisioning_requires_paid(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        await make_user(uow, 1)
        payment = await uow.payments.add(make_payment(1, seeded_tariffs[0].id))
        await uow.commit()

        too_early = await uow.payments.mark_provisioned(payment.id, NOW)
        await uow.payments.mark_paid(payment.id, NOW, NOW + timedelta(days=30))
        now_ok = await uow.payments.mark_provisioned(payment.id, NOW)
        await uow.commit()

        assert too_early is None
        assert now_ok is not None
        assert now_ok.status == PaymentStatus.PROVISIONED

    async def test_expiring_a_paid_invoice_is_refused(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        """Money already taken must never be swept into "expired"."""
        await make_user(uow, 1)
        payment = await uow.payments.add(make_payment(1, seeded_tariffs[0].id))
        await uow.commit()
        await uow.payments.mark_paid(payment.id, NOW, NOW + timedelta(days=30))
        await uow.commit()

        assert await uow.payments.mark_expired(payment.id) is None

    async def test_provider_invoice_id_is_unique_per_provider(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        await make_user(uow, 1)
        tariff_id = seeded_tariffs[0].id
        await uow.payments.add(
            make_payment(1, tariff_id, provider_invoice_id='inv-1')
        )
        await uow.payments.add(
            make_payment(
                1,
                tariff_id,
                provider=PaymentProvider.CRYPTOBOT,
                provider_invoice_id='inv-1',
            )
        )
        await uow.commit()

        with pytest.raises(IntegrityError):
            await uow.payments.add(
                make_payment(1, tariff_id, provider_invoice_id='inv-1')
            )

    async def test_queues_split_by_status_and_ttl(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        await make_user(uow, 1)
        tariff_id = seeded_tariffs[0].id
        live = await uow.payments.add(make_payment(1, tariff_id))
        stale = await uow.payments.add(
            make_payment(
                1, tariff_id, invoice_expires_at=NOW - timedelta(minutes=1)
            )
        )
        paid = await uow.payments.add(make_payment(1, tariff_id))
        await uow.commit()
        await uow.payments.mark_paid(paid.id, NOW, NOW + timedelta(days=30))
        await uow.commit()

        pending = await uow.payments.list_pending(NOW)
        expiring = await uow.payments.list_stale_pending(NOW)
        awaiting = await uow.payments.list_awaiting_provisioning()

        assert [p.id for p in pending] == [live.id]
        assert [p.id for p in expiring] == [stale.id]
        assert [p.id for p in awaiting] == [paid.id]

    async def test_late_payment_sweep_window(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        await make_user(uow, 1)
        tariff_id = seeded_tariffs[0].id
        yesterday = await uow.payments.add(
            make_payment(
                1, tariff_id, invoice_expires_at=NOW - timedelta(hours=20)
            )
        )
        long_ago = await uow.payments.add(
            make_payment(
                1, tariff_id, invoice_expires_at=NOW - timedelta(days=5)
            )
        )
        await uow.commit()
        for payment in (yesterday, long_ago):
            await uow.payments.mark_expired(payment.id)
        await uow.commit()

        found = await uow.payments.list_recently_expired(
            NOW - timedelta(days=1), NOW
        )

        assert [p.id for p in found] == [yesterday.id]

    async def test_unknown_payment_lock_returns_none(
        self, uow: UnitOfWork
    ) -> None:
        assert await uow.payments.lock_for_finalize(uuid.uuid4()) is None

    async def test_second_worker_skips_a_locked_payment(
        self, session_factory, seeded_tariffs, uow: UnitOfWork
    ) -> None:
        """The whole point of SKIP LOCKED: a concurrent "проверить
        оплату" tap must bounce instead of queueing behind the poller.

        SQLite silently ignores FOR UPDATE, which is why these tests
        insist on real PostgreSQL.
        """
        await make_user(uow, 1)
        payment = await uow.payments.add(make_payment(1, seeded_tariffs[0].id))
        await uow.commit()

        async with UnitOfWork(session_factory) as holder:
            locked = await holder.payments.lock_for_finalize(payment.id)
            assert locked is not None

            async with UnitOfWork(session_factory) as contender:
                assert (
                    await contender.payments.lock_for_finalize(payment.id)
                    is None
                )


async def test_seeded_tariffs_carry_a_device_count(
    uow: UnitOfWork, seeded_tariffs
) -> None:
    """Four-device plans sit next to the existing ones, same durations."""
    two = await uow.tariffs.get_by_code('m1')
    four = await uow.tariffs.get_by_code('m1x4')

    assert two is not None
    assert four is not None
    assert two.max_devices == 2
    assert four.max_devices == 4
    assert four.duration_days == two.duration_days
    assert four.price_kopeks == 32_000
