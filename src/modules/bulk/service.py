from __future__ import annotations

import asyncio
import time
import uuid

import structlog

from src.core.ports import (
    BulkNotifierPort,
    CachePort,
    CheckOutcome,
    FacebookCheckerPort,
    JobQueuePort,
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
    def __init__(
        self,
        *,
        checker: FacebookCheckerPort,
        cache: CachePort,
        queue: JobQueuePort | None,
        concurrency: int = 50,
        cache_ttl_seconds: int = 600,
        max_bulk_uids: int = 50000,
        progress_interval_seconds: float = 2.5,
    ) -> None:
        self._checker = checker
        self._cache = cache
        self._queue = queue
        self._concurrency = max(1, concurrency)
        self._cache_ttl = max(0, cache_ttl_seconds)
        self._max_bulk_uids = max_bulk_uids
        self._progress_interval = max(0.5, progress_interval_seconds)

    async def check_single_uid(self, raw_uid: str) -> tuple[str, CheckOutcome, bool]:
        uid = extract_uid(raw_uid)
        if not uid:
            raise ValueError("UID không hợp lệ (cần 5–20 chữ số hoặc link profile.php?id=...).")

        cached = await self._cache.get_cached_outcomes([uid])
        if uid in cached:
            return uid, cached[uid], True

        result = await self._checker.check(uid)
        await self._cache.set_cached_outcome(uid, result.outcome, self._cache_ttl)
        return uid, result.outcome, False

    async def enqueue_bulk_text(
        self,
        chat_id: int,
        text: str,
        status_message_id: int,
    ) -> tuple[str, int, int]:
        if self._queue is None:
            raise RuntimeError("ARQ job queue is not configured.")

        uids, skipped = extract_uids_from_text(text, max_uids=self._max_bulk_uids)
        if not uids:
            raise ValueError("Không tìm thấy UID hợp lệ nào trong nội dung gửi lên.")

        job_id = uuid.uuid4().hex[:8]
        await self._cache.save_bulk_payload(job_id, uids, ttl_seconds=3600)
        await self._queue.enqueue_bulk_check(job_id, chat_id, status_message_id)
        logger.info("bulk_job_enqueued", job_id=job_id, chat_id=chat_id, total=len(uids))
        return job_id, len(uids), skipped

    async def run_bulk_job(
        self,
        job_id: str,
        chat_id: int,
        status_message_id: int,
        notifier: BulkNotifierPort,
    ) -> BulkSummary | None:
        uids = await self._cache.load_bulk_payload(job_id)
        if not uids:
            logger.warning("bulk_job_payload_missing", job_id=job_id, chat_id=chat_id)
            return None

        started = time.monotonic()
        total = len(uids)
        cached = await self._cache.get_cached_outcomes(uids)
        cache_hits = len(cached)

        live_uids: list[str] = []
        die_uids: list[str] = []
        error_uids: list[str] = []
        pending_uids: list[str] = []

        for uid in uids:
            outcome = cached.get(uid)
            if outcome == CheckOutcome.LIVE:
                live_uids.append(uid)
            elif outcome == CheckOutcome.DIE:
                die_uids.append(uid)
            else:
                pending_uids.append(uid)

        done = cache_hits
        last_update = 0.0
        new_cache_entries: dict[str, CheckOutcome] = {}

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
                    cache_hits=cache_hits,
                )
                await notifier.update_progress(chat_id, status_message_id, text)

        await _maybe_update(force=True)

        sem = asyncio.Semaphore(self._concurrency)

        async def _check_one(target_uid: str) -> tuple[str, CheckOutcome]:
            async with sem:
                res = await self._checker.check(target_uid)
                return target_uid, res.outcome

        tasks = [asyncio.create_task(_check_one(uid)) for uid in pending_uids]
        try:
            for coro in asyncio.as_completed(tasks):
                uid, outcome = await coro
                if outcome == CheckOutcome.LIVE:
                    live_uids.append(uid)
                    new_cache_entries[uid] = outcome
                elif outcome == CheckOutcome.DIE:
                    die_uids.append(uid)
                    new_cache_entries[uid] = outcome
                else:
                    error_uids.append(uid)
                done += 1
                await _maybe_update(force=False)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        if new_cache_entries:
            await self._cache.set_cached_outcomes(new_cache_entries, self._cache_ttl)
        await self._cache.delete_bulk_payload(job_id)

        elapsed = time.monotonic() - started
        summary = BulkSummary(
            job_id=job_id,
            total=total,
            live=len(live_uids),
            die=len(die_uids),
            error=len(error_uids),
            cache_hits=cache_hits,
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
            cache_hits=cache_hits,
            elapsed=round(elapsed, 2),
        )
        return summary
