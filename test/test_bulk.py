from __future__ import annotations

from collections.abc import Sequence

import fakeredis.aioredis
import pytest

from src.core.ports import CheckOutcome, UidCheckResult
from src.core.redis import RedisCache
from src.modules.bulk.domain import (
    build_combined_result_file,
    build_result_files,
    extract_uid,
    extract_uids_from_text,
    render_progress_bar,
)
from src.modules.bulk.service import BulkCheckService


class _StubChecker:
    def __init__(self, outcomes: dict[str, CheckOutcome]) -> None:
        self._outcomes = outcomes
        self.called: list[str] = []

    async def check(self, uid: str) -> UidCheckResult:
        self.called.append(uid)
        return UidCheckResult(uid=uid, outcome=self._outcomes.get(uid, CheckOutcome.DIE))

    async def close(self) -> None:
        return None


class _StubQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, int, int]] = []

    async def enqueue_bulk_check(self, job_id: str, chat_id: int, status_message_id: int) -> None:
        self.enqueued.append((job_id, chat_id, status_message_id))


class _StubBulkNotifier:
    def __init__(self) -> None:
        self.progress_updates: list[str] = []
        self.results: list[tuple[list[str], list[str], list[str]]] = []

    async def update_progress(self, chat_id: int, message_id: int, text: str) -> None:
        self.progress_updates.append(text)

    async def send_bulk_results(
        self,
        chat_id: int,
        caption: str,
        live_uids: Sequence[str],
        die_uids: Sequence[str],
        error_uids: Sequence[str],
        job_id: str,
        status_message_id: int | None = None,
    ) -> None:
        self.results.append((list(live_uids), list(die_uids), list(error_uids)))


def test_extract_uids_deduplicates_large_input_and_progress_bar() -> None:
    assert extract_uid("100012345678901") == "100012345678901"
    assert (
        extract_uid("https://www.facebook.com/profile.php?id=100012345678901") == "100012345678901"
    )
    assert extract_uid("not-a-uid") is None

    lines = [str(1000000000 + i) for i in range(10000)]
    lines.extend(["1000000000", "invalid_row", "https://fb.com/profile.php?id=1000000001"])
    uids, skipped = extract_uids_from_text("\n".join(lines))
    assert len(uids) == 10000
    assert skipped == 3

    assert render_progress_bar(5, 10, width=10) == "[█████░░░░░] 50%"
    files = dict(build_result_files(["10001"], ["10002"], []))
    assert set(files.keys()) == {"live.txt", "die.txt"}
    fname, content = build_combined_result_file(["10001"], ["10002"], [], "abc123")
    assert fname == "result_abc123.txt"
    decoded = content.decode("utf-8")
    assert "=== LIVE (1) ===" in decoded and "=== DIE (1) ===" in decoded


@pytest.mark.asyncio
async def test_bulk_check_service_uses_cache_and_sends_result_files() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = RedisCache(redis, prefix="test")
    queue = _StubQueue()
    checker = _StubChecker(
        {
            "100002": CheckOutcome.LIVE,
            "100003": CheckOutcome.DIE,
            "100004": CheckOutcome.ERROR,
        }
    )
    svc = BulkCheckService(
        checker=checker,
        cache=cache,
        queue=queue,
        concurrency=10,
        cache_ttl_seconds=600,
        progress_interval_seconds=0.5,
    )

    # Pre-seed 100001 in Redis cache
    await cache.set_cached_outcome("100001", CheckOutcome.LIVE, 600)

    uid, outcome, from_cache = await svc.check_single_uid("100001")
    assert (uid, outcome, from_cache) == ("100001", CheckOutcome.LIVE, True)

    job_id, total, skipped = await svc.enqueue_bulk_text(
        chat_id=555,
        text="100001\n100002\n100003\n100004\n100001\n",
        status_message_id=77,
    )
    assert total == 4
    assert skipped == 1
    assert len(queue.enqueued) == 1

    notifier = _StubBulkNotifier()
    summary = await svc.run_bulk_job(job_id, chat_id=555, status_message_id=77, notifier=notifier)
    assert summary is not None
    assert summary.total == 4
    assert summary.cache_hits == 1
    assert summary.live == 2
    assert summary.die == 1
    assert summary.error == 1
    assert "100001" not in checker.called
    assert len(notifier.progress_updates) >= 1
    assert len(notifier.results) == 1
    assert set(notifier.results[0][0]) == {"100001", "100002"}
    assert notifier.results[0][1] == ["100003"]
    assert notifier.results[0][2] == ["100004"]

    # Bulk payload cleaned up after job finishes
    assert await cache.load_bulk_payload(job_id) == []

    await redis.aclose()
