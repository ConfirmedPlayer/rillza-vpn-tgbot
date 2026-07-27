"""Admin broadcasts, resumable by design.

The message is copied rather than forwarded, so it arrives from the bot
with no trace of who wrote it. Progress is checkpointed by user id, so a
restart continues where it stopped instead of spamming everyone again.
"""

from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from loguru import logger

from app.core.enums import BroadcastStatus
from app.db.models import Broadcast
from app.services.uow import UnitOfWork

#: Users per page, and how often progress is written down.
PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class BroadcastReport:
    sent: int
    failed: int
    blocked: int


class BroadcastService:
    def __init__(self, uow: UnitOfWork, bot: Bot) -> None:
        self._uow = uow
        self._bot = bot

    async def create(
        self, content_chat_id: int, content_message_id: int
    ) -> Broadcast:
        broadcast = Broadcast(
            content_chat_id=content_chat_id,
            content_message_id=content_message_id,
            status=BroadcastStatus.DRAFT,
        )
        await self._uow.broadcasts.add(broadcast)
        await self._uow.commit()
        return broadcast

    async def run(self, broadcast: Broadcast, sleep=None) -> BroadcastReport:
        """Send to everyone, resuming from the stored cursor."""
        broadcast.status = BroadcastStatus.RUNNING
        await self._uow.commit()

        while True:
            users = await self._uow.users.iter_broadcast_targets(
                after_id=broadcast.last_user_id, limit=PAGE_SIZE
            )
            if not users:
                break

            for user in users:
                await self._send_one(broadcast, user.id, sleep)
                broadcast.last_user_id = user.id
            # Checkpoint once per page rather than per message.
            await self._uow.commit()

        broadcast.status = BroadcastStatus.DONE
        await self._uow.commit()
        logger.info(
            'Broadcast {} finished: {} sent, {} blocked, {} failed',
            broadcast.id,
            broadcast.sent,
            broadcast.blocked,
            broadcast.failed,
        )
        return BroadcastReport(
            sent=broadcast.sent,
            failed=broadcast.failed,
            blocked=broadcast.blocked,
        )

    async def _send_one(
        self, broadcast: Broadcast, telegram_id: int, sleep
    ) -> None:
        try:
            await self._copy(telegram_id, broadcast)
        except TelegramRetryAfter as error:
            # Respect flood control, then give this user one more try.
            if sleep is not None:
                await sleep(error.retry_after + 1)
            try:
                await self._copy(telegram_id, broadcast)
            except Exception as retry_error:
                logger.warning(
                    'Broadcast to {} failed after retry: {}',
                    telegram_id,
                    retry_error,
                )
                broadcast.failed += 1
                return
        except TelegramForbiddenError:
            # The user blocked the bot: stop counting them as reachable.
            await self._uow.users.set_bot_blocked(telegram_id, True)
            broadcast.blocked += 1
            return
        except Exception as error:
            logger.warning('Broadcast to {} failed: {}', telegram_id, error)
            broadcast.failed += 1
            return
        broadcast.sent += 1

    async def _copy(self, telegram_id: int, broadcast: Broadcast) -> None:
        await self._bot.copy_message(
            chat_id=telegram_id,
            from_chat_id=broadcast.content_chat_id,
            message_id=broadcast.content_message_id,
        )
