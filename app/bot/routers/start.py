"""The /start command.

The real menu (trial, purchase, subscription screen) lands in later
phases; this keeps the skeleton runnable end to end.
"""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.texts import ru

router = Router(name='start')
# Group and channel updates are dropped: this bot is private-chat only.
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(ru.START)
