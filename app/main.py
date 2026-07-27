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

from app.bot.routers import ROUTERS
from app.core.logging import add_telegram_sink, setup_logging
from app.core.settings import Settings, get_settings


def _dumps(value: object) -> str:
    return orjson.dumps(value).decode()


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_storage(settings: Settings) -> BaseStorage:
    return RedisStorage.from_url(
        settings.redis_url, json_loads=orjson.loads, json_dumps=_dumps
    )


def build_dispatcher(
    settings: Settings, storage: BaseStorage | None = None
) -> Dispatcher:
    """Wire the dispatcher; tests pass an in-memory storage."""
    dispatcher = Dispatcher(storage=storage or build_storage(settings))
    dispatcher.include_routers(*ROUTERS)
    return dispatcher


async def run() -> None:
    settings = get_settings()
    setup_logging(settings)

    bot = build_bot(settings)
    dispatcher = build_dispatcher(settings)

    log_bot: Bot | None = None
    if settings.telegram_logging_enabled:
        assert settings.log_bot_token is not None
        log_bot = Bot(token=settings.log_bot_token.get_secret_value())
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


def main() -> None:
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(run())


if __name__ == '__main__':
    main()
