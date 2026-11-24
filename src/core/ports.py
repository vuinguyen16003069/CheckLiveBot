from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class CheckOutcome(StrEnum):
    LIVE = "LIVE"
    DIE = "DIE"
    ERROR = "ERROR"


class UidCheckResult(BaseModel):
    uid: str
    outcome: CheckOutcome
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str | None = None


class FacebookCheckerPort(Protocol):
    async def check(self, uid: str) -> UidCheckResult: ...
    async def verify_live(self, uid: str) -> UidCheckResult: ...
    async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]: ...
    async def close(self) -> None: ...


class LockPort(Protocol):
    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool: ...
    async def release_lock(self, key: str) -> None: ...


class BulkNotifierPort(Protocol):
    async def update_progress(self, chat_id: int, message_id: int, text: str) -> None: ...
    async def send_bulk_results(
        self,
        chat_id: int,
        caption: str,
        live_uids: Sequence[str],
        die_uids: Sequence[str],
        error_uids: Sequence[str],
        job_id: str,
        status_message_id: int | None = None,
    ) -> None: ...
