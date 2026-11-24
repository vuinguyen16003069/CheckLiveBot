from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class ThrottlingMiddleware(BaseMiddleware):
    """Zero-latency in-memory sliding-window rate limiter (0 Redis commands)."""

    def __init__(self, limit_per_minute: int = 30) -> None:
        self._limit = limit_per_minute
        self._windows: dict[int, deque[float]] = {}

    def check_rate_limit(self, user_id: int) -> bool:
        if self._limit <= 0:
            return True
        now = time.monotonic()
        cutoff = now - 60.0
        window = self._windows.get(user_id)
        if window is None:
            window = deque[float]()
            self._windows[user_id] = window
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self._limit:
            return False
        window.append(now)
        # Opportunistically prune idle users when map grows large
        if len(self._windows) > 2048:
            stale = [uid for uid, w in self._windows.items() if not w or w[-1] <= cutoff]
            for uid in stale:
                del self._windows[uid]
        return True

    def retry_after_seconds(self, user_id: int) -> int:
        if self._limit <= 0:
            return 0
        window = self._windows.get(user_id)
        if not window or len(window) < self._limit:
            return 0
        remaining = 60.0 - (time.monotonic() - window[0])
        return max(1, math.ceil(remaining))

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        if isinstance(event, (Message, CallbackQuery)) and event.from_user:
            user_id = event.from_user.id

        if user_id is not None and not self.check_rate_limit(user_id):
            wait = self.retry_after_seconds(user_id)
            if isinstance(event, Message):
                await event.answer(
                    f"⏳ Bạn thao tác hơi nhanh, vui lòng chờ {wait} giây rồi thử lại."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    f"⏳ Thao tác quá nhanh, thử lại sau {wait} giây.",
                    show_alert=False,
                )
            return None
        return await handler(event, data)
