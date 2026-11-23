from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from typing import Any

import structlog

from src.core.ports import (
    BulkNotifierPort,
    CheckOutcome,
    FacebookCheckerPort,
)
from src.modules.bulk.domain import (
    BulkSummary,
    extract_uid,
    extract_uids_from_text,
    format_bulk_progress,
    format_bulk_summary,
)

logger = structlog.get_logger(__name__)


class BulkCheckService:
    """In-memory high-throughput bulk UID checker with bounded worker pool (0 Redis overhead)."""

    def __init__(
        self,
        *,
        checker: FacebookCheckerPort,
        concurrency: int = 100,
        max_bulk_uids: int = 50000,
        progress_interval_seconds: float = 1.5,
    ) -> None:
        self._checker = checker
        self._concurrency = max(1, concurrency)
        self._max_bulk_uids = max_bulk_uids
        self._progress_interval = max(0.5, progress_interval_seconds)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._active_chats: set[int] = set()

    def is_chat_busy(self, chat_id: int) -> bool:
        return chat_id in self._active_chats

    async def check_single_uid(self, raw_uid: str) -> tuple[str, CheckOutcome]:
        uid = extract_uid(raw_uid)
        if not uid:
            raise ValueError("UID không hợp lệ (cần 1–20 chữ số hoặc link Facebook cá nhân).")

        result = await self._checker.check(uid)
        return uid, result.outcome

    def prepare_bulk_text(self, text: str) -> tuple[str, list[str], int]:
        uids, skipped = extract_uids_from_text(text, max_uids=self._max_bulk_uids)
        if not uids:
            raise ValueError("Không tìm thấy UID hợp lệ nào trong nội dung gửi lên.")
        return uuid.uuid4().hex[:8], uids, skipped

    def start_bulk_uids(
        self,
        *,
        job_id: str,
        chat_id: int,
        status_message_id: int,
        uids: Sequence[str],
        notifier: BulkNotifierPort,
    ) -> None:
        if chat_id in self._active_chats:
            raise ValueError("⏳ Chat đang có 1 phiên check hàng loạt chạy, vui lòng chờ hoàn tất.")
        self._active_chats.add(chat_id)

        async def _runner() -> None:
            try:
                await self.run_bulk_uids(
                    job_id=job_id,
                    chat_id=chat_id,
                    status_message_id=status_message_id,
                    uids=uids,
                    notifier=notifier,
                )
            except Exception as exc:
                logger.exception("bulk_job_failed", job_id=job_id, error=str(exc))
            finally:
                self._active_chats.discard(chat_id)

        task = asyncio.create_task(_runner(), name=f"bulk:{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _t: self._active_chats.discard(chat_id))
        logger.info("bulk_job_started", job_id=job_id, chat_id=chat_id, total=len(uids))

    def start_bulk_text(
        self,
        *,
        chat_id: int,
        text: str,
        status_message_id: int,
        notifier: BulkNotifierPort,
    ) -> tuple[str, int, int]:
        job_id, uids, skipped = self.prepare_bulk_text(text)
        self.start_bulk_uids(
            job_id=job_id,
            chat_id=chat_id,
            status_message_id=status_message_id,
            uids=uids,
            notifier=notifier,
        )
        return job_id, len(uids), skipped

    async def run_bulk_uids(
        self,
        *,
        job_id: str,
        chat_id: int,
        status_message_id: int,
        uids: Sequence[str],
        notifier: BulkNotifierPort,
    ) -> BulkSummary:
        started = time.monotonic()
        total = len(uids)

        live_uids: list[str] = []
        die_uids: list[str] = []
        error_uids: list[str] = []

        done = 0
        last_update = 0.0

        async def _maybe_update(force: bool = False) -> None:
            nonlocal last_update
            now = time.monotonic()
            if force or (now - last_update) >= self._progress_interval:
                last_update = now
                text = format_bulk_progress(
                    job_id=job_id,
                    done=done,
                    total=total,
                    live=len(live_uids),
                    die=len(die_uids),
                    error=len(error_uids),
                )
                await notifier.update_progress(chat_id, status_message_id, text)

        uid_queue: asyncio.Queue[str] = asyncio.Queue()
        for uid in uids:
            uid_queue.put_nowait(uid)

        async def _worker() -> None:
            nonlocal done
            while True:
                try:
                    target_uid = uid_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    res = await self._checker.check(target_uid)
                    if res.outcome == CheckOutcome.LIVE:
                        live_uids.append(target_uid)
                    elif res.outcome == CheckOutcome.DIE:
                        die_uids.append(target_uid)
                    else:
                        error_uids.append(target_uid)
                    done += 1
                    await _maybe_update(force=False)
                finally:
                    uid_queue.task_done()

        worker_count = min(self._concurrency, total)
        workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
        try:
            await asyncio.gather(*workers)
        finally:
            for w in workers:
                if not w.done():
                    w.cancel()

        elapsed = time.monotonic() - started
        summary = BulkSummary(
            job_id=job_id,
            total=total,
            live=len(live_uids),
            die=len(die_uids),
            error=len(error_uids),
            elapsed_seconds=elapsed,
        )

        caption = format_bulk_summary(summary)
        await notifier.send_bulk_results(
            chat_id=chat_id,
            caption=caption,
            live_uids=live_uids,
            die_uids=die_uids,
            error_uids=error_uids,
            job_id=job_id,
            status_message_id=status_message_id,
        )
        logger.info(
            "bulk_job_completed",
            job_id=job_id,
            chat_id=chat_id,
            total=total,
            live=summary.live,
            die=summary.die,
            error=summary.error,
            elapsed=round(elapsed, 2),
        )
        return summary

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._active_chats.clear()
