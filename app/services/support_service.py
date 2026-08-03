"""Anonymous support: the user writes, the admins answer invisibly.

Everything is relayed with ``copy_message``, never forwarded. A forward
carries a "переслано от" header that names the sender, which is exactly
what the previous bot leaked when the owner answered — the whole point
of this feature is that the person replying stays unseen.

Routing works by reply: each inbound message is copied into the admins'
chats and the resulting message ids are remembered, so an admin can
simply reply to the card and the answer finds its way back. That removes
the old bot's single global "current dialog", which sent the answer to
the wrong person whenever two people wrote at once.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, auto

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from loguru import logger

from app.bot import keyboards
from app.bot.texts import support as texts
from app.core.enums import SupportDirection
from app.core.settings import Settings
from app.db.models import SupportMessage
from app.services.rate_limit import RateLimiter
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.uow import UnitOfWork

#: How many messages one user may send per window.
RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 60
#: A reply after this long re-introduces itself as support.
HEADER_SILENCE = timedelta(hours=6)


class SupportUserUnreachable(Exception):
    """The user blocked the bot between writing in and being answered.

    Distinct from "no thread": the admin wrote a real reply to a real
    ticket, and telling them the message was unroutable would send them
    looking for a bug that is not there.
    """

    def __init__(self, telegram_id: int) -> None:
        super().__init__(telegram_id)
        self.telegram_id = telegram_id


class RelayOutcome(Enum):
    SENT = auto()
    BLOCKED = auto()
    TOO_FAST = auto()
    #: No admin could be reached — the message is stored, not lost.
    UNDELIVERED = auto()


@dataclass(frozen=True, slots=True)
class RelayResult:
    outcome: RelayOutcome
    delivered_to: int = 0


class SupportService:
    def __init__(
        self,
        uow: UnitOfWork,
        bot: Bot,
        settings: Settings,
        subscriptions: SubscriptionService,
        limiter: RateLimiter,
    ) -> None:
        self._uow = uow
        self._bot = bot
        self._settings = settings
        self._subscriptions = subscriptions
        self._limiter = limiter

    # --- user -> admins ----------------------------------------------

    async def relay_from_user(
        self, telegram_id: int, chat_id: int, message_id: int
    ) -> RelayResult:
        user = await self._uow.users.get(telegram_id)
        if user is not None and user.support_blocked:
            return RelayResult(RelayOutcome.BLOCKED)

        allowed = await self._limiter.allow(
            f'support:{telegram_id}', RATE_LIMIT, RATE_WINDOW_SECONDS
        )
        if not allowed:
            return RelayResult(RelayOutcome.TOO_FAST)

        card = await self._render_card(telegram_id)
        delivered = 0
        for admin_id in self._settings.admin_ids:
            if await self._deliver_to_admin(
                admin_id, telegram_id, chat_id, message_id, card
            ):
                delivered += 1

        await self._uow.commit()
        if delivered == 0:
            logger.warning(
                'Support message from {} reached no admin', telegram_id
            )
            return RelayResult(RelayOutcome.UNDELIVERED)
        return RelayResult(RelayOutcome.SENT, delivered)

    async def _deliver_to_admin(
        self,
        admin_id: int,
        telegram_id: int,
        chat_id: int,
        message_id: int,
        card: str,
    ) -> bool:
        try:
            header = await self._bot.send_message(
                admin_id,
                card,
                reply_markup=keyboards.support_card(telegram_id),
            )
            copy = await self._bot.copy_message(
                chat_id=admin_id, from_chat_id=chat_id, message_id=message_id
            )
        except TelegramAPIError as error:
            logger.warning(
                'Support delivery to admin {} failed: {}', admin_id, error
            )
            return False

        self._remember(
            telegram_id, admin_id, header.message_id, copy.message_id
        )
        return True

    # --- a request the bot composed on the user's behalf --------------

    async def relay_composed(self, telegram_id: int, text: str) -> RelayResult:
        """File a ticket the bot wrote on the user's behalf.

        ``relay_from_user`` copies a message the user actually sent;
        a canned request has none. It travels as a plain send instead,
        which is safe in this direction: forwarding is banned because
        it would name the owner when *answering*, and the admin card
        already names the person writing in.

        Everything else is the same gate: the support block, the rate
        limiter — otherwise the button becomes a way around it — and
        the same SupportMessage rows, so replies route by reply.
        """
        user = await self._uow.users.get(telegram_id)
        if user is not None and user.support_blocked:
            return RelayResult(RelayOutcome.BLOCKED)

        allowed = await self._limiter.allow(
            f'support:{telegram_id}', RATE_LIMIT, RATE_WINDOW_SECONDS
        )
        if not allowed:
            return RelayResult(RelayOutcome.TOO_FAST)

        card = await self._render_card(telegram_id)
        delivered = 0
        for admin_id in self._settings.admin_ids:
            if await self._deliver_composed(admin_id, telegram_id, card, text):
                delivered += 1

        await self._uow.commit()
        if delivered == 0:
            logger.warning(
                'Composed request from {} reached no admin', telegram_id
            )
            return RelayResult(RelayOutcome.UNDELIVERED)
        return RelayResult(RelayOutcome.SENT, delivered)

    async def _deliver_composed(
        self, admin_id: int, telegram_id: int, card: str, text: str
    ) -> bool:
        try:
            header = await self._bot.send_message(
                admin_id,
                card,
                reply_markup=keyboards.support_card(telegram_id),
            )
            body = await self._bot.send_message(admin_id, text)
        except TelegramAPIError as error:
            logger.warning(
                'Composed delivery to admin {} failed: {}', admin_id, error
            )
            return False

        self._remember(
            telegram_id, admin_id, header.message_id, body.message_id
        )
        return True

    def _remember(
        self, telegram_id: int, admin_id: int, *message_ids: int
    ) -> None:
        """Replying to any of these must route back to the same user."""
        for admin_message_id in message_ids:
            self._uow.session.add(
                SupportMessage(
                    user_id=telegram_id,
                    admin_chat_id=admin_id,
                    admin_message_id=admin_message_id,
                    direction=SupportDirection.IN,
                )
            )

    async def _render_card(self, telegram_id: int) -> str:
        user = await self._uow.users.get(telegram_id)
        subscription = await self._subscriptions.get(telegram_id)
        payments = await self._uow.payments.list_by_user(telegram_id, limit=3)
        return texts.render_card(user, subscription, payments, utcnow())

    # --- admin -> user -----------------------------------------------

    async def relay_to_user(
        self, admin_chat_id: int, reply_to_message_id: int, message_id: int
    ) -> int | None:
        """Send an admin's reply on, anonymously. Returns the recipient."""
        thread = await self._uow.support.find_recipient(
            admin_chat_id, reply_to_message_id
        )
        if thread is None:
            return None

        try:
            if await self._needs_header(thread):
                await self._bot.send_message(thread, texts.REPLY_HEADER)

            sent = await self._bot.copy_message(
                chat_id=thread,
                from_chat_id=admin_chat_id,
                message_id=message_id,
            )
        except TelegramForbiddenError:
            # They blocked the bot after writing in. Record it so they
            # drop out of broadcasts, and tell the admin what happened
            # instead of surfacing a generic "что-то пошло не так".
            await self._uow.users.set_bot_blocked(thread, True)
            await self._uow.commit()
            logger.info('Support reply refused: {} blocked the bot', thread)
            raise SupportUserUnreachable(thread) from None
        self._uow.session.add(
            SupportMessage(
                user_id=thread,
                admin_chat_id=admin_chat_id,
                admin_message_id=message_id,
                direction=SupportDirection.OUT,
            )
        )
        await self._uow.commit()
        logger.info(
            'Support reply delivered to {} (message {})',
            thread,
            sent.message_id,
        )
        return thread

    async def _needs_header(self, telegram_id: int) -> bool:
        """Introduce the reply when the conversation has gone quiet."""
        last = await self._uow.support.last_outbound_at(telegram_id)
        return last is None or utcnow() - last > HEADER_SILENCE

    # --- moderation ---------------------------------------------------

    async def set_blocked(self, telegram_id: int, blocked: bool) -> None:
        await self._uow.users.set_support_blocked(
            telegram_id, utcnow() if blocked else None
        )
        await self._uow.commit()
