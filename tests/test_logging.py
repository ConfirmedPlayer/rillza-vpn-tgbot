from types import SimpleNamespace

import pytest

from app.core.logging import (
    DEDUP_WINDOW_SECONDS,
    TELEGRAM_MESSAGE_LIMIT,
    build_telegram_sink,
    split_message,
)


def test_short_message_is_one_chunk() -> None:
    assert split_message('hello') == ['hello']


def test_empty_message_still_yields_one_chunk() -> None:
    assert split_message('') == ['']


def test_message_at_exactly_the_limit_is_not_split() -> None:
    text = 'x' * TELEGRAM_MESSAGE_LIMIT
    assert split_message(text) == [text]


def test_oversized_message_is_split_and_not_dropped() -> None:
    """Regression: the legacy sink silently dropped oversized logs.

    Its chunker used ``range(len(text), limit)``, which is empty whenever
    the text is longer than the limit — so nothing was ever sent.
    """
    text = 'y' * (TELEGRAM_MESSAGE_LIMIT * 2 + 17)

    chunks = split_message(text)

    assert len(chunks) == 3
    assert all(len(chunk) <= TELEGRAM_MESSAGE_LIMIT for chunk in chunks)
    assert ''.join(chunks) == text


def test_join_of_chunks_reproduces_input() -> None:
    text = ''.join(str(i % 10) for i in range(10_000))
    assert ''.join(split_message(text)) == text


def test_non_positive_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        split_message('anything', limit=0)


class _Msg(str):
    """Stands in for loguru's message: a str that carries its record."""

    def __new__(cls, text: str, level: str = 'ERROR', line: int = 1) -> '_Msg':
        obj = super().__new__(cls, text)
        obj.record = {  # type: ignore[attr-defined]
            'level': SimpleNamespace(name=level),
            'name': 'app.services.payment_service',
            'function': 'poll_pending',
            'line': line,
        }
        return obj


class _Bot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: object, text: str) -> None:
        self.sent.append(text)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestTelegramSinkUnderAStorm:
    """The alert channel is the owner's only window once the bot lives
    on a VPS. A two-hour panel outage makes one 30-second poller emit
    hundreds of identical errors; Telegram flood-controls the chat, and
    the channel spends the next half hour replaying an outage that is
    already over. Seen once, it stops being read."""

    async def test_repeats_of_one_error_are_counted_not_sent(self) -> None:
        bot, clock = _Bot(), _Clock()
        sink = build_telegram_sink(bot, 1, clock=clock)

        for _ in range(200):
            clock.now += 1.0
            await sink(_Msg('panel unreachable'))

        assert len(bot.sent) == 1
        assert 'panel unreachable' in bot.sent[0]

    async def test_distinct_errors_still_get_through(self) -> None:
        bot, clock = _Bot(), _Clock()
        sink = build_telegram_sink(bot, 1, clock=clock)

        await sink(_Msg('panel unreachable', line=10))
        await sink(_Msg('provider refused', line=20))

        assert len(bot.sent) == 2

    async def test_the_suppressed_count_is_reported_afterwards(self) -> None:
        bot, clock = _Bot(), _Clock()
        sink = build_telegram_sink(bot, 1, clock=clock)

        for _ in range(47):
            await sink(_Msg('panel unreachable'))
        clock.now += DEDUP_WINDOW_SECONDS + 1
        await sink(_Msg('что-то ещё', line=99))

        summary = [t for t in bot.sent if '46' in t]
        assert summary, f'нет сводки о подавленных: {bot.sent}'

    async def test_it_works_with_a_real_loguru_record(self) -> None:
        """The tests above feed a stand-in. This one proves the stand-in
        matches what loguru actually hands the sink."""
        from loguru import logger

        bot, clock = _Bot(), _Clock()
        handler = logger.add(
            build_telegram_sink(bot, 1, clock=clock),
            level='ERROR',
            format='{message}',
        )
        try:
            for _ in range(5):
                logger.error('панель не отвечает')
            await logger.complete()
        finally:
            logger.remove(handler)

        assert len(bot.sent) == 1
