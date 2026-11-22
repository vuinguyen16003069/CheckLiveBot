from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.core.ports import CachePort


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, cache: CachePort, limit_per_minute: int = 30) -> None:
        self._cache = cache
        self._limit = limit_per_minute

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        if isinstance(event, (Message, CallbackQuery)) and event.from_user:
            user_id = event.from_user.id

        if user_id is not None and self._limit > 0:
            allowed = await self._cache.check_rate_limit(user_id, self._limit)
            if not allowed:
                if isinstance(event, Message):
                    await event.answer(
                        "⏳ Bạn thao tác hơi nhanh, vui lòng chờ vài giây rồi thử lại."
                    )
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        "⏳ Thao tác quá nhanh, thử lại sau vài giây.",
                        show_alert=False,
                    )
                return None
        return await handler(event, data)
