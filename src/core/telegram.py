from __future__ import annotations

import contextlib

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat


async def is_actor_authorized(
    bot: Bot | None,
    chat: Chat,
    actor_user_id: int,
    owner_user_id: int,
) -> bool:
    """In groups/supergroups, restrict destructive actions to the owner or a group admin."""
    if chat.type not in ("group", "supergroup"):
        return True
    if owner_user_id == 0 or actor_user_id == owner_user_id:
        return True
    if bot is None or actor_user_id <= 0:
        return False
    with contextlib.suppress(TelegramAPIError):
        member = await bot.get_chat_member(chat.id, actor_user_id)
        return str(getattr(member, "status", "")) in ("administrator", "creator")
    return False
