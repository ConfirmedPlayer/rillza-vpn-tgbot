"""Application entrypoint: build everything, run polling, shut down cleanly."""

import asyncio
from contextlib import suppress
from datetime import timedelta

import orjson
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.middlewares.database import DatabaseMiddleware
from app.bot.middlewares.services import ServicesMiddleware
from app.bot.middlewares.user_upsert import UserUpsertMiddleware
from app.bot.routers import build_routers
from app.core.logging import add_telegram_sink, setup_logging
from app.core.settings import Settings, get_settings
from app.db.engine import build_engine, build_session_factory
from app.integrations.celerity import CelerityClient
from app.integrations.payments import PaymentRegistry
from app.scheduler.jobs import JobRunner, register_jobs
from app.services.rate_limit import RateLimiter, RedisRateLimiter


def _dumps(value: object) -> str:
    return orjson.dumps(value).decode()


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_log_bot(settings: Settings) -> Bot | None:
    """The second bot used for the Telegram log sink, if configured."""
    if not settings.telegram_logging_enabled or settings.log_bot_token is None:
        return None
    return Bot(token=settings.log_bot_token.get_secret_value())


#: How long an untouched FSM key survives. Every dialog here is a
#: single screen someone may simply walk away from — «Поддержка» opened
#: and never written into, an admin price prompt abandoned — and without
#: a TTL each of those leaves a key that nothing ever deletes. A day is
#: far longer than any of these flows and short enough that the leak
#: stops being permanent.
FSM_TTL = timedelta(days=1)


def build_storage(settings: Settings) -> BaseStorage:
    return RedisStorage.from_url(
        settings.redis_url,
        json_loads=orjson.loads,
        json_dumps=_dumps,
        state_ttl=FSM_TTL,
        data_ttl=FSM_TTL,
    )


def build_dispatcher(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    panel: CelerityClient,
    providers: PaymentRegistry | None = None,
    storage: BaseStorage | None = None,
    limiter: RateLimiter | None = None,
) -> Dispatcher:
    """Wire the dispatcher.

    Tests pass an in-memory storage and a fake panel.
    """
    dispatcher = Dispatcher(storage=storage or build_storage(settings))
    dispatcher.include_routers(*build_routers(settings))

    # Outer middlewares so the unit of work also covers filters.
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.outer_middleware(DatabaseMiddleware(session_factory))
        observer.outer_middleware(UserUpsertMiddleware())
        observer.outer_middleware(
            ServicesMiddleware(
                settings, panel, providers or PaymentRegistry({}), limiter
            )
        )

    return dispatcher


async def run() -> None:
    settings = get_settings()
    setup_logging(settings)

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    panel = CelerityClient(settings)
    providers = PaymentRegistry.from_settings(settings)
    bot = build_bot(settings)
    storage = build_storage(settings)
    dispatcher = build_dispatcher(
        settings,
        session_factory,
        panel,
        providers,
        storage=storage,
        limiter=RedisRateLimiter(storage.redis),
    )

    scheduler = AsyncIOScheduler()
    register_jobs(
        scheduler, JobRunner(session_factory, settings, panel, providers, bot)
    )

    log_bot = build_log_bot(settings)
    if log_bot is not None:
        add_telegram_sink(settings, log_bot)

    try:
        scheduler.start()
        logger.info(
            'Rillza VPN bot started polling; payment providers: {}',
            ', '.join(providers.available()) or 'none configured',
        )
        await start_polling(dispatcher, bot)
    finally:
        # The scheduler goes first: closing the bot session under a
        # running job left a resuming broadcast marking its whole
        # remaining audience 'failed' and then DONE, so nobody past the
        # cursor ever heard from it. wait=False because these jobs run
        # on this very loop — waiting on them here would deadlock.
        if scheduler.running:
            scheduler.shutdown(wait=False)
        # Let the Telegram sink flush before its bot session is closed.
        await logger.complete()
        await dispatcher.storage.close()
        await bot.session.close()
        if log_bot is not None:
            await log_bot.session.close()
        await panel.close()
        await providers.close()
        await engine.dispose()


async def start_polling(dispatcher, bot) -> None:
    """Poll until stopped, leaving the bot session for run() to close.

    aiogram closes it inside ``start_polling``'s own finally when
    ``close_bot_session`` is left at its default of True — and that
    finally completes before ours begins. So the ordering run() spells
    out was not actually happening: the session was already closed by
    the time the scheduler was told to stop, which is precisely the
    situation that ordering exists to prevent. run() closes it itself,
    after the scheduler, where the comment says it does.
    """
    await dispatcher.start_polling(bot, close_bot_session=False)


def main() -> None:
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(run())


if __name__ == '__main__':
    main()
