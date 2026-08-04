"""Who closes the bot session, and when.

``run()``'s finally block spells out an order and gives the reason: the
scheduler is stopped first, because closing the bot session under a
running job left a resuming broadcast marking its whole remaining
audience 'failed' and then DONE — nobody past the cursor ever heard from
it. That order only holds if nothing closes the session earlier.
"""

from app.main import start_polling


class RecordingDispatcher:
    """Stands in for aiogram's Dispatcher, recording how it was called."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def start_polling(self, bot, **kwargs) -> None:
        self.kwargs = kwargs


async def test_polling_leaves_the_bot_session_for_the_caller() -> None:
    """aiogram closes the session inside start_polling's own finally.

    ``close_bot_session`` defaults to True, and the close runs in
    aiogram's finally — which completes before ours ever starts. So the
    session was already gone by the time the scheduler was told to stop,
    and the ordering documented in run() was not actually happening: a
    broadcast running at shutdown hit a closed session, which is the
    exact failure that ordering exists to prevent.

    Ours closes it, after the scheduler, where the comment says it does.
    """
    dispatcher = RecordingDispatcher()

    await start_polling(dispatcher, object())

    assert dispatcher.kwargs is not None
    assert dispatcher.kwargs.get('close_bot_session') is False
