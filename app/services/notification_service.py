"""Expiry reminders.

Two notices per subscription: three days out and one day out. Which one
already went is recorded on the subscription itself, so a restart, a
misfire or an overlapping run cannot send it twice — and a renewal
clears the marker, starting the cycle over.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from loguru import logger

from app.bot import keyboards
from app.bot.texts import ru
from app.core.enums import NotifiedStage, SubscriptionOrigin
from app.services.subscription_service import utcnow
from app.services.uow import UnitOfWork

#: (marker, days left) — checked in order, so the nearer notice wins.
STAGES = ((NotifiedStage.ONE_DAY, 1), (NotifiedStage.THREE_DAYS, 3))


@dataclass(frozen=True, slots=True)
class ReminderReport:
    sent: int = 0
    blocked: int = 0
    failed: int = 0


class NotificationService:
    def __init__(self, uow: UnitOfWork, bot: Bot) -> None:
        self._uow = uow
        self._bot = bot

    async def send_expiry_reminders(
        self, now: datetime | None = None
    ) -> ReminderReport:
        now = now or utcnow()
        sent = blocked = failed = 0

        for stage, days in STAGES:
            window_end = now + timedelta(days=days)
            window_start = window_end - timedelta(days=1)
            due = await self._uow.subscriptions.list_expiring_between(
                max(now, window_start), window_end
            )
            for subscription in due:
                if subscription.notified_stage == stage:
                    continue
                term = subscription.expires_at - subscription.created_at
                if (
                    subscription.origin == SubscriptionOrigin.TRIAL
                    # Whole days only: created_at is the transaction
                    # clock, so the term is never exactly N days.
                    and term.days <= days
                ):
                    # A trial shorter than the horizon lands inside this
                    # window the hour it is granted: "продлите, чтобы не
                    # остаться без доступа" an hour after a gift reads
                    # as a scam. The nearer stage still fires, which is
                    # the nudge that converts. A longer trial keeps both.
                    continue
                # Claim the notice before sending: a crash mid-send costs
                # one reminder, a double send costs trust.
                claimed = await self._uow.subscriptions.mark_notified(
                    subscription.id, stage
                )
                await self._uow.commit()
                if claimed is None:
                    continue

                outcome = await self._notify(subscription.user_id, days, now)
                if outcome == 'sent':
                    sent += 1
                elif outcome == 'blocked':
                    blocked += 1
                else:
                    failed += 1

        return ReminderReport(sent=sent, blocked=blocked, failed=failed)

    async def _notify(self, telegram_id: int, days: int, now: datetime) -> str:
        left = ru.format_left(now + timedelta(days=days), now)
        text = ru.EXPIRES_SOON.format(left=left)
        try:
            await self._bot.send_message(
                telegram_id, text, reply_markup=keyboards.expiring_soon()
            )
        except TelegramRetryAfter as error:
            # Re-raising aborted the whole pass, and the stage is already
            # claimed, so every reminder after this one was lost too —
            # not just the one that hit the limit.
            logger.warning('Flood control while reminding {}', telegram_id)
            await asyncio.sleep(error.retry_after + 1)
            try:
                await self._bot.send_message(
                    telegram_id, text, reply_markup=keyboards.expiring_soon()
                )
            except Exception as retry_error:
                logger.warning(
                    'Reminder to {} failed after the wait: {}',
                    telegram_id,
                    retry_error,
                )
                return 'failed'
            return 'sent'
        except TelegramForbiddenError:
            await self._uow.users.set_bot_blocked(telegram_id, True)
            await self._uow.commit()
            return 'blocked'
        except Exception as error:  # a reminder must not kill the job
            logger.warning('Reminder to {} failed: {}', telegram_id, error)
            return 'failed'
        return 'sent'
