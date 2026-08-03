"""Logging setup: stderr always, a Telegram chat optionally.

The Telegram sink is the place the legacy bot got wrong: it built chunks
with ``range(len(text), 4096)``, which is empty whenever the message is
longer than the limit, so every oversized log was silently dropped. Here
:func:`split_message` is the tested primitive that does it correctly.
"""

import asyncio
import sys
from collections.abc import Callable, Coroutine
from time import monotonic
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from loguru import logger

from app.core.settings import Settings

#: Telegram rejects messages longer than this many characters.
TELEGRAM_MESSAGE_LIMIT = 4096
#: Repeats of one error inside this window are counted, not sent.
DEDUP_WINDOW_SECONDS = 600
#: How a tally of swallowed repeats introduces itself.
SUPPRESSED_PREFIX = '⏱ Повторов подавлено:'

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


def _signature(message: Any) -> tuple[Any, ...]:
    """What makes two records "the same error" for deduplication."""
    record = getattr(message, 'record', None)
    if not record:
        return (str(message)[:200],)
    level = record.get('level')
    return (
        getattr(level, 'name', str(level)),
        record.get('name'),
        record.get('function'),
        record.get('line'),
    )


def build_telegram_sink(
    bot: Bot, chat_id: int | str, clock: Callable[[], float] = monotonic
) -> Callable[[Any], Coroutine[Any, Any, None]]:
    """Build an async loguru sink forwarding records to a Telegram chat.

    Sends are serialised by a lock so chunks of one record stay together
    and arrive in order.

    Repeats are counted rather than sent. This channel is the owner's
    only window once the bot runs unattended, and an outage makes it
    useless exactly when it matters: a two-hour panel failure gives one
    30-second poller some 240 identical errors, Telegram flood-controls
    the chat, and the queue then replays an outage that is already over.
    A channel that behaves like that once stops being read.

    So the first occurrence of a signature goes out immediately, further
    ones inside :data:`DEDUP_WINDOW_SECONDS` are tallied, and the tally
    is reported when the window closes. Reporting is lazy — it rides on
    the next record — so if logging stops altogether the final tally is
    never sent. That is deliberate: nothing is happening to report on,
    and a background timer here would outlive the sink it belongs to.
    """
    lock = asyncio.Lock()
    #: signature -> when it was last sent
    sent_at: dict[tuple[Any, ...], float] = {}
    #: signature -> repeats swallowed since then, and one sample line
    tally: dict[tuple[Any, ...], tuple[int, str]] = {}

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

    async def _send_text(text: str) -> None:
        for chunk in split_message(text):
            await _send(chunk)

    async def _flush_expired(now: float) -> None:
        for signature, when in list(sent_at.items()):
            if now - when < DEDUP_WINDOW_SECONDS:
                continue
            del sent_at[signature]
            count, sample = tally.pop(signature, (0, ''))
            if count:
                await _send_text(
                    f'{SUPPRESSED_PREFIX} {count} за последние '
                    f'{DEDUP_WINDOW_SECONDS // 60} мин:\n\n{sample}'
                )

    async def sink(message: Any) -> None:
        text = str(message)
        signature = _signature(message)
        async with lock:
            now = clock()
            await _flush_expired(now)
            if signature in sent_at:
                count, sample = tally.get(signature, (0, text))
                tally[signature] = (count + 1, sample)
                return
            sent_at[signature] = now
            await _send_text(text)

    return sink


def add_telegram_sink(settings: Settings, log_bot: Bot) -> None:
    """Attach the Telegram sink; no-op unless it is configured.

    Uses the same predicate as the caller so a half-filled configuration
    can never attach a sink pointed at an empty chat id.
    """
    chat_id = settings.log_chat_id
    if not settings.telegram_logging_enabled or chat_id is None:
        return
    logger.add(
        build_telegram_sink(log_bot, chat_id),
        level=settings.log_telegram_level,
        format=TELEGRAM_FORMAT,
        colorize=False,
        backtrace=False,
        diagnose=False,
    )
