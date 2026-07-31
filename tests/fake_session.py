"""A Bot session that records API calls instead of performing them.

The ``timeout`` parameters are dictated by aiogram's BaseSession
contract, hence the ASYNC109 exemption.
"""

# ruff: noqa: ASYNC109

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import CopyMessage, SendMessage, TelegramMethod
from aiogram.types import Chat, Message, MessageId, User

BOT_ID = 123456789
FAKE_TOKEN = f'{BOT_ID}:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0'


class RecordingSession(BaseSession):
    """Captures every outgoing method call for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        #: Answer every call the way Telegram answers a blocked bot.
        self.forbidden = False

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.requests.append(method)
        if self.forbidden:
            raise TelegramForbiddenError(
                method=method, message='bot was blocked by the user'
            )
        if isinstance(method, CopyMessage):
            # copy_message answers with the new message's id only.
            return MessageId(message_id=len(self.requests))
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type='private'),
                from_user=User(id=BOT_ID, is_bot=True, first_name='Rillza'),
                text=method.text,
            )
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        yield b''

    async def close(self) -> None:
        return None

    def sent_texts(self) -> list[str]:
        return [
            request.text
            for request in self.requests
            if isinstance(request, SendMessage)
        ]
