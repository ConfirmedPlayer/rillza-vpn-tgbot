"""Logging setup: stderr always, a Telegram chat optionally.

The Telegram sink is the place the legacy bot got wrong: it built chunks
with ``range(len(text), 4096)``, which is empty whenever the message is
longer than the limit, so every oversized log was silently dropped. Here
:func:`split_message` is the tested primitive that does it correctly.
"""

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from loguru import logger

from app.core.settings import Settings

#: Telegram rejects messages longer than this many characters.
TELEGRAM_MESSAGE_LIMIT = 4096

STDERR_FORMAT = (
    '<green>{time:DD.MM.YYYY HH:mm:ss}</green> | '
    '<level>{level: <8}</level> | '
    '<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — '
    '<level>{message}</level>'
)
TELEGRAM_FORMAT = (
    '{level} | {time:DD.MM.YYYY HH:mm:ss}\n'
    '{name}:{function}:{line}\n\n'
    '{message}'
)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Always returns at least one chunk, and joining the result reproduces
    the input exactly.
    """
    if limit < 1:
        raise ValueError('limit must be positive')
    if not text:
        return ['']
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _emergency(message: str) -> None:
    """Report a sink failure without going through loguru (no recursion)."""
    print(f'[logging] {message}', file=sys.stderr, flush=True)


def setup_logging(settings: Settings) -> None:
    """Reset loguru and install the stderr sink."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=STDERR_FORMAT,
        backtrace=True,
        diagnose=False,
    )


def build_telegram_sink(
    bot: Bot, chat_id: int | str
) -> Callable[[Any], Coroutine[Any, Any, None]]:
    """Build an async loguru sink forwarding records to a Telegram chat.

    Sends are serialised by a lock so chunks of one record stay together
    and arrive in order.
    """
    lock = asyncio.Lock()

    async def _send(chunk: str) -> None:
        try:
            await bot.send_message(chat_id, chunk)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await bot.send_message(chat_id, chunk)
            except Exception as retry_error:
                _emergency(f'telegram sink retry failed: {retry_error!r}')
        except Exception as error:
            _emergency(f'telegram sink failed: {error!r}')

    async def sink(message: Any) -> None:
        text = str(message)
        async with lock:
            for chunk in split_message(text):
                await _send(chunk)

    return sink


def add_telegram_sink(settings: Settings, log_bot: Bot) -> None:
    """Attach the Telegram sink; no-op unless it is configured."""
    if settings.log_chat_id is None:
        return
    logger.add(
        build_telegram_sink(log_bot, settings.log_chat_id),
        level=settings.log_telegram_level,
        format=TELEGRAM_FORMAT,
        colorize=False,
        backtrace=False,
        diagnose=False,
    )
