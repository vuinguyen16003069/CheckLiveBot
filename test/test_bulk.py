from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from src.core.ports import CheckOutcome, UidCheckResult
from src.modules.bulk.domain import (
    build_combined_result_file,
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

    async def verify_live(self, uid: str) -> UidCheckResult:
        return await self.check(uid)

    async def close(self) -> None:
        return None


class _StubBulkNotifier:
    def __init__(self) -> None:
        self.progress_updates: list[str] = []
        self.results: list[tuple[list[str], list[str], list[str]]] = []
        self.done = asyncio.Event()

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
        self.done.set()


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
    fname, content = build_combined_result_file(["10001"], ["10002"], [], "abc123")
    assert fname == "result_abc123.txt"
    decoded = content.decode("utf-8")
    assert "=== LIVE (1) ===" in decoded
    assert "=== DIE (1) ===" in decoded


@pytest.mark.asyncio
async def test_bulk_check_service_runs_in_memory_and_sends_results() -> None:
    checker = _StubChecker(
        {
            "100001": CheckOutcome.LIVE,
            "100002": CheckOutcome.LIVE,
            "100003": CheckOutcome.DIE,
            "100004": CheckOutcome.ERROR,
        }
    )
    svc = BulkCheckService(
        checker=checker,
        concurrency=10,
        progress_interval_seconds=0.5,
    )

    uid, outcome = await svc.check_single_uid("100001")
    assert (uid, outcome) == ("100001", CheckOutcome.LIVE)
    assert checker.called == ["100001"]

    notifier = _StubBulkNotifier()
    job_id, total, skipped = svc.start_bulk_text(
        chat_id=555,
        text="100001\n100002\n100003\n100004\n100001\n",
        status_message_id=77,
        notifier=notifier,
    )
    assert job_id
    assert total == 4
    assert skipped == 1

    await asyncio.wait_for(notifier.done.wait(), timeout=5.0)
    assert checker.called.count("100001") == 2
    assert len(notifier.progress_updates) >= 1
    assert len(notifier.results) == 1
    assert set(notifier.results[0][0]) == {"100001", "100002"}
    assert notifier.results[0][1] == ["100003"]
    assert notifier.results[0][2] == ["100004"]

    await svc.close()
