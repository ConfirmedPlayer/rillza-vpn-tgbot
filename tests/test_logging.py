import pytest

from app.core.logging import TELEGRAM_MESSAGE_LIMIT, split_message


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
