from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Sequence
from typing import Any

from src.core.ports import (
    BulkNotifierPort,
    CheckOutcome,
    FacebookCheckerPort,
)
from src.modules.bulk.domain import (
    BulkSummary,
    extract_fb_url,
    extract_uid,
    extract_uids_from_text,
    format_bulk_progress,
    format_bulk_summary,
)

logger = logging.getLogger(__name__)


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
        self._jobs: dict[str, tuple[int, asyncio.Task[Any]]] = {}

    @property
    def max_bulk_uids(self) -> int:
        return self._max_bulk_uids

    def is_chat_busy(self, chat_id: int) -> bool:
        return any(cid == chat_id for cid, _ in self._jobs.values())

    def cancel_bulk_job(self, chat_id: int, job_id: str | None = None) -> bool:
        if job_id is not None:
            entry = self._jobs.get(job_id)
            if entry is None or entry[0] != chat_id:
                return False
            self._jobs.pop(job_id, None)
            entry[1].cancel()
            logger.info("bulk_job_cancel_requested job_id=%s chat_id=%s", job_id, chat_id)
            return True
        targets = [jid for jid, (cid, _) in self._jobs.items() if cid == chat_id]
        if not targets:
            return False
        for jid in targets:
            _, task = self._jobs.pop(jid)
            task.cancel()
            logger.info("bulk_job_cancel_requested job_id=%s chat_id=%s", jid, chat_id)
        return True

    async def resolve_input_uid(self, raw_uid: str) -> str | None:
        uid = extract_uid(raw_uid)
        if uid:
            return uid
        fb_url = extract_fb_url(raw_uid)
        if fb_url:
            return await self._checker.resolve_uid_from_url(fb_url)
        return None

    async def check_single_uid(self, raw_uid: str) -> tuple[str, CheckOutcome]:
        uid = await self.resolve_input_uid(raw_uid)
        if not uid:
            raise ValueError(
                "Không tìm thấy UID hợp lệ (cần UID 1–20 chữ số hoặc link trang cá nhân Facebook công khai)."
            )

        result = await self._checker.check(uid)
        return uid, result.outcome

    async def check_single_uid_profile(
        self, raw_uid: str
    ) -> tuple[str, CheckOutcome, str | None, str | None]:
        uid = await self.resolve_input_uid(raw_uid)
        if not uid:
            raise ValueError(
                "Không tìm thấy UID hợp lệ (cần UID 1–20 chữ số hoặc link trang cá nhân Facebook công khai)."
            )

        profile_task = asyncio.create_task(self._checker.fetch_profile_info(uid))
        try:
            result = await self._checker.check(uid)
            if result.outcome != CheckOutcome.LIVE:
                profile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await profile_task
                return uid, result.outcome, None, None
            fb_name, avatar_url = await profile_task
            return uid, result.outcome, fb_name, avatar_url
        except BaseException:
            if not profile_task.done():
                profile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await profile_task
            raise

    async def fetch_avatar_bytes(self, url: str) -> bytes | None:
        return await self._checker.fetch_avatar_bytes(url)

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
        def _release_job() -> None:
            self._jobs.pop(job_id, None)

        async def _runner() -> None:
            try:
                await self.run_bulk_uids(
                    job_id=job_id,
                    chat_id=chat_id,
                    status_message_id=status_message_id,
                    uids=uids,
                    notifier=notifier,
                )
            except asyncio.CancelledError:
                logger.info("bulk_job_cancelled job_id=%s chat_id=%s", job_id, chat_id)
                raise
            except Exception as exc:
                logger.exception("bulk_job_failed job_id=%s error=%s", job_id, exc)
                with contextlib.suppress(Exception):
                    await notifier.update_progress(
                        chat_id,
                        status_message_id,
                        f"⚠️ Phiên kiểm tra <code>#{job_id}</code> gặp sự cố, vui lòng thử lại.",
                    )
            finally:
                _release_job()

        task = asyncio.create_task(_runner(), name=f"bulk:{job_id}")
        self._jobs[job_id] = (chat_id, task)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _t: _release_job())
        logger.info("bulk_job_started job_id=%s chat_id=%s total=%s", job_id, chat_id, len(uids))

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
        uid_iter = iter(uids)
        workers_done = asyncio.Event()

        async def _push_progress() -> None:
            text = format_bulk_progress(
                job_id=job_id,
                done=done,
                total=total,
                live=len(live_uids),
                die=len(die_uids),
                error=len(error_uids),
                elapsed_seconds=time.monotonic() - started,
            )
            await notifier.update_progress(chat_id, status_message_id, text)

        async def _progress_reporter() -> None:
            last_sent = 0
            next_wait = min(0.35, self._progress_interval)
            while not workers_done.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(workers_done.wait(), timeout=next_wait)
                if done > last_sent:
                    last_sent = done
                    await _push_progress()
                next_wait = self._progress_interval
            if done > last_sent:
                await _push_progress()

        async def _worker() -> None:
            nonlocal done
            for target_uid in uid_iter:
                res = await self._checker.check(target_uid)
                if res.outcome == CheckOutcome.LIVE:
                    live_uids.append(target_uid)
                elif res.outcome == CheckOutcome.DIE:
                    die_uids.append(target_uid)
                else:
                    error_uids.append(target_uid)
                done += 1

        worker_count = min(self._concurrency, total)
        reporter_task = asyncio.create_task(_progress_reporter(), name=f"bulk-progress:{job_id}")
        workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            workers_done.set()
            for w in workers:
                if not w.done():
                    w.cancel()
            if not reporter_task.done():
                reporter_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter_task
            raise
        else:
            workers_done.set()
            await reporter_task

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
            "bulk_job_completed job_id=%s chat_id=%s total=%s live=%s die=%s error=%s elapsed=%.2f",
            job_id,
            chat_id,
            total,
            summary.live,
            summary.die,
            summary.error,
            elapsed,
        )
        return summary

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._jobs.clear()
