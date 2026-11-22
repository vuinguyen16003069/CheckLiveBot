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
    async def close(self) -> None: ...


class CachePort(Protocol):
    async def get_cached_outcomes(self, uids: Sequence[str]) -> dict[str, CheckOutcome]: ...
    async def set_cached_outcome(
        self, uid: str, outcome: CheckOutcome, ttl_seconds: int
    ) -> None: ...
    async def set_cached_outcomes(
        self,
        outcomes: dict[str, CheckOutcome],
        ttl_seconds: int,
    ) -> None: ...
    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool: ...
    async def check_rate_limit(self, user_id: int, limit_per_minute: int) -> bool: ...
    async def save_bulk_payload(
        self,
        job_id: str,
        uids: Sequence[str],
        ttl_seconds: int = 3600,
    ) -> None: ...
    async def load_bulk_payload(self, job_id: str) -> list[str]: ...
    async def delete_bulk_payload(self, job_id: str) -> None: ...


class JobQueuePort(Protocol):
    async def enqueue_bulk_check(
        self,
        job_id: str,
        chat_id: int,
        status_message_id: int,
    ) -> None: ...


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
    ) -> None: ...
