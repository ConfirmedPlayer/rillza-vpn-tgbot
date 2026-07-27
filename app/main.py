"""Application entrypoint: build everything, run polling, shut down cleanly."""

import asyncio
from contextlib import suppress

import orjson
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
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


def build_storage(settings: Settings) -> BaseStorage:
    return RedisStorage.from_url(
        settings.redis_url, json_loads=orjson.loads, json_dumps=_dumps
    )


def build_dispatcher(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    panel: CelerityClient,
    storage: BaseStorage | None = None,
) -> Dispatcher:
    """Wire the dispatcher.

    Tests pass an in-memory storage and a fake panel.
    """
    dispatcher = Dispatcher(storage=storage or build_storage(settings))
    dispatcher.include_routers(*build_routers())

    # Outer middlewares so the unit of work also covers filters.
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.outer_middleware(DatabaseMiddleware(session_factory))
        observer.outer_middleware(UserUpsertMiddleware())
        observer.outer_middleware(ServicesMiddleware(settings, panel))

    return dispatcher


async def run() -> None:
    settings = get_settings()
    setup_logging(settings)

    engine = build_engine(settings)
    panel = CelerityClient(settings)
    bot = build_bot(settings)
    dispatcher = build_dispatcher(
        settings, build_session_factory(engine), panel
    )

    log_bot = build_log_bot(settings)
    if log_bot is not None:
        add_telegram_sink(settings, log_bot)

    try:
        logger.info('Rillza VPN bot started polling')
        await dispatcher.start_polling(bot)
    finally:
        # Let the Telegram sink flush before its bot session is closed.
        await logger.complete()
        await dispatcher.storage.close()
        await bot.session.close()
        if log_bot is not None:
            await log_bot.session.close()
        await panel.close()
        await engine.dispose()


def main() -> None:
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(run())


if __name__ == '__main__':
    main()
