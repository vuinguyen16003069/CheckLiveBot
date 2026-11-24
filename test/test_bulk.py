from __future__ import annotations

import asyncio
from collections.abc import Sequence
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot

from src.core.ports import CheckOutcome, UidCheckResult
from src.modules.bulk.domain import (
    BulkSummary,
    build_combined_result_file,
    extract_uid,
    extract_uids_from_text,
    format_bulk_progress,
    format_bulk_summary,
    format_single_check,
    render_progress_bar,
)
from src.modules.bulk.handlers import TelegramBulkNotifier, build_single_check_markup
from src.modules.bulk.service import BulkCheckService


class _StubChecker:
    def __init__(
        self,
        outcomes: dict[str, CheckOutcome],
        *,
        delay: float = 0.0,
        fail_uid: str | None = None,
    ) -> None:
        self._outcomes = outcomes
        self._delay = delay
        self._fail_uid = fail_uid
        self.called: list[str] = []

    async def check(self, uid: str) -> UidCheckResult:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._fail_uid == uid:
            raise RuntimeError("boom")
        self.called.append(uid)
        return UidCheckResult(uid=uid, outcome=self._outcomes.get(uid, CheckOutcome.DIE))

    async def verify_live(self, uid: str) -> UidCheckResult:
        return await self.check(uid)

    async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
        return (
            ("Vui Nguyễn", "https://scontent.xx.fbcdn.net/avatar.jpg")
            if self._outcomes.get(uid) == CheckOutcome.LIVE
            else (None, None)
        )

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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4", "4"),
        ("100012345678901", "100012345678901"),
        ("9" * 20, "9" * 20),
        ("9" * 21, None),
        ("0", None),
        ("012345", None),
        ("", None),
        ("   ", None),
        ("not-a-uid", None),
        ("https://www.facebook.com/profile.php?id=4", "4"),
        ("https://m.facebook.com/profile.php?foo=1&id=100012345678901&bar=2", "100012345678901"),
        ("HTTPS://FB.COM/PROFILE.PHP?ID=20531316728", "20531316728"),
    ],
)
def test_extract_uid_edge_cases(raw: str, expected: str | None) -> None:
    assert extract_uid(raw) == expected


def test_extract_uids_deduplicates_caps_and_formats() -> None:
    lines = [str(1000000000 + i) for i in range(10000)]
    lines.extend(["1000000000", "invalid_row", "https://fb.com/profile.php?id=1000000001"])
    uids, skipped = extract_uids_from_text("\n".join(lines))
    assert len(uids) == 10000
    assert skipped == 3

    # Embedded numeric token extraction ignores leading-zero tokens + respects max_uids cap
    mixed = "\n\n00000\n012345\nuser 100011111111111 note\n100022222222222\n100033333333333\n"
    capped, cap_skipped = extract_uids_from_text(mixed, max_uids=2)
    assert capped == ["100011111111111", "100022222222222"]
    assert cap_skipped == 2

    # Progress bar boundary clamping
    assert render_progress_bar(0, 0, width=4) == "[░░░░] 0%"
    assert render_progress_bar(-5, 10, width=4) == "[░░░░] 0%"
    assert render_progress_bar(5, 10, width=10) == "[█████░░░░░] 50%"
    assert render_progress_bar(99, 10, width=4) == "[████] 100%"

    assert "#job1" in format_bulk_progress("job1", 5, 10, 2, 2, 1)
    speed_progress = format_bulk_progress("job1", 500, 1000, 300, 150, 50, elapsed_seconds=2.5)
    assert "200 UID/s" in speed_progress
    assert "ETA" in speed_progress
    summary = BulkSummary(job_id="job1", total=10, live=6, die=3, error=1, elapsed_seconds=1.25)
    assert "1.2s" in format_bulk_summary(summary) or "1.3s" in format_bulk_summary(summary)
    assert "LIVE" in format_single_check("4", CheckOutcome.LIVE)
    assert "Vui Nguyễn" in format_single_check("4", CheckOutcome.LIVE, fb_name="Vui Nguyễn")
    assert "DIE" in format_single_check("4", CheckOutcome.DIE)
    assert "LỖI" in format_single_check("4", CheckOutcome.ERROR)

    # Combined result file sections
    fname_empty, empty_bytes = build_combined_result_file([], [], [], "empty")
    assert (fname_empty, empty_bytes) == ("result_empty.txt", b"")

    fname, content = build_combined_result_file(["10001"], ["10002"], ["10003"], "abc123")
    assert fname == "result_abc123.txt"
    decoded = content.decode("utf-8")
    assert "=== LIVE (1) ===" in decoded
    assert "=== DIE (1) ===" in decoded
    assert "=== ERROR (1) ===" in decoded


def test_single_check_markup_and_notifier_behavior() -> None:
    live_kb = build_single_check_markup("4", CheckOutcome.LIVE)
    assert [b.text for b in live_kb.inline_keyboard[0]] == ["🔄 Check lại"]

    die_kb = build_single_check_markup("10001", CheckOutcome.DIE)
    assert [b.text for b in die_kb.inline_keyboard[0]] == ["➕ Lên kèo UID này", "🔄 Check lại"]


@pytest.mark.asyncio
async def test_telegram_bulk_notifier_sends_doc_or_text() -> None:
    bot = AsyncMock(spec=Bot)
    notifier = TelegramBulkNotifier(bot)

    await notifier.update_progress(10, 20, "progress")
    bot.edit_message_text.assert_awaited_once()

    await notifier.send_bulk_results(10, "cap", ["10001"], [], [], "j1", status_message_id=20)
    bot.delete_message.assert_awaited_once_with(chat_id=10, message_id=20)
    bot.send_document.assert_awaited_once()

    await notifier.send_bulk_results(10, "empty_cap", [], [], [], "j2", status_message_id=None)
    bot.send_message.assert_awaited_once_with(chat_id=10, text="empty_cap")


@pytest.mark.asyncio
async def test_bulk_check_service_runs_guards_and_closes_cleanly() -> None:
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

    with pytest.raises(ValueError, match="UID không hợp lệ"):
        await svc.check_single_uid("invalid")

    with pytest.raises(ValueError, match="Không tìm thấy UID"):
        svc.prepare_bulk_text("no uids here\nonly text")

    uid, outcome = await svc.check_single_uid("100001")
    assert (uid, outcome) == ("100001", CheckOutcome.LIVE)
    assert checker.called == ["100001"]

    notifier = _StubBulkNotifier()
    other_notifier = _StubBulkNotifier()
    job_id, total, skipped = svc.start_bulk_text(
        chat_id=555,
        text="100001\n100002\n100003\n100004\n100001\n",
        status_message_id=77,
        notifier=notifier,
    )
    assert job_id
    assert total == 4
    assert skipped == 1
    assert svc.is_chat_busy(555) is True
    assert svc.is_chat_busy(666) is False

    # Same chat is blocked while active; different chat is allowed concurrently
    with pytest.raises(ValueError, match="đang có 1 phiên"):
        svc.start_bulk_text(
            chat_id=555,
            text="100001\n100002\n",
            status_message_id=78,
            notifier=notifier,
        )
    svc.start_bulk_text(
        chat_id=666,
        text="100001\n100003\n",
        status_message_id=79,
        notifier=other_notifier,
    )

    await asyncio.wait_for(notifier.done.wait(), timeout=5.0)
    await asyncio.wait_for(other_notifier.done.wait(), timeout=5.0)
    await asyncio.sleep(0)
    assert svc.is_chat_busy(555) is False
    assert svc.is_chat_busy(666) is False
    assert len(notifier.progress_updates) >= 1
    assert len(notifier.results) == 1
    assert set(notifier.results[0][0]) == {"100001", "100002"}
    assert notifier.results[0][1] == ["100003"]
    assert notifier.results[0][2] == ["100004"]

    # Unexpected failure inside runner must still release chat busy guard & report error
    failing_notifier = _StubBulkNotifier()
    failing_svc = BulkCheckService(
        checker=_StubChecker({}, fail_uid="100001"),
        concurrency=2,
    )
    failing_svc.start_bulk_text(
        chat_id=999,
        text="100001\n100002\n",
        status_message_id=80,
        notifier=failing_notifier,
    )
    await asyncio.sleep(0.05)
    assert failing_svc.is_chat_busy(999) is False
    assert any("gặp sự cố" in msg for msg in failing_notifier.progress_updates)

    # cancel_bulk_job stops in-flight job cleanly and releases chat busy guard
    slow_svc = BulkCheckService(checker=_StubChecker({}, delay=5.0), concurrency=2)
    slow_job_id, _, _ = slow_svc.start_bulk_text(
        chat_id=888,
        text="100001\n100002\n",
        status_message_id=81,
        notifier=_StubBulkNotifier(),
    )
    assert slow_svc.is_chat_busy(888) is True
    assert slow_svc.cancel_bulk_job(888, "wrong-id") is False
    assert slow_svc.cancel_bulk_job(888, slow_job_id) is True
    assert slow_svc.is_chat_busy(888) is False
    assert slow_svc.cancel_bulk_job(888, slow_job_id) is False
    await slow_svc.close()
    await svc.close()


@pytest.mark.asyncio
async def test_telegram_bulk_notifier_respects_retry_after_cooldown() -> None:
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import EditMessageText

    bot = AsyncMock(spec=Bot)
    bot.edit_message_text.side_effect = TelegramRetryAfter(
        method=EditMessageText(chat_id=10, message_id=20, text="p"),
        message="Too Many Requests",
        retry_after=30,
    )
    notifier = TelegramBulkNotifier(bot, job_id="job42")

    await notifier.update_progress(10, 20, "progress 1")
    await notifier.update_progress(10, 20, "progress 2")
    # Second call skipped during flood-wait cooldown
    assert bot.edit_message_text.await_count == 1
