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


@pytest.mark.asyncio
async def test_bulk_notifier_send_document_retries_on_retry_after() -> None:
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendDocument

    bot = AsyncMock(spec=Bot)
    bot.send_document.side_effect = [
        TelegramRetryAfter(
            method=SendDocument(chat_id=10, document="f"),
            message="Too Many Requests",
            retry_after=0,
        ),
        None,
    ]
    notifier = TelegramBulkNotifier(bot, job_id="j9")
    await notifier.send_bulk_results(10, "cap", ["10001"], [], [], "j9", status_message_id=None)
    assert bot.send_document.await_count == 2


@pytest.mark.asyncio
async def test_check_single_uid_profile_fetches_on_live_and_cancels_on_die() -> None:
    cancelled_uids: list[str] = []

    class _SlowProfileChecker(_StubChecker):
        async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
            if self._outcomes.get(uid) == CheckOutcome.LIVE:
                return "Mark Zuckerberg", "https://scontent.xx.fbcdn.net/zuck.jpg"
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                cancelled_uids.append(uid)
                raise
            return None, None

    svc = BulkCheckService(
        checker=_SlowProfileChecker({"4": CheckOutcome.LIVE, "8": CheckOutcome.DIE}, delay=0.01)
    )
    assert svc.max_bulk_uids == 50000

    with pytest.raises(ValueError, match="UID không hợp lệ"):
        await svc.check_single_uid_profile("not-valid")

    uid_live, out_live, name_live, avt_live = await svc.check_single_uid_profile("4")
    assert (uid_live, out_live, name_live, avt_live) == (
        "4",
        CheckOutcome.LIVE,
        "Mark Zuckerberg",
        "https://scontent.xx.fbcdn.net/zuck.jpg",
    )

    # DIE must cancel in-flight profile fetch immediately instead of waiting 5s
    uid_die, out_die, name_die, avt_die = await asyncio.wait_for(
        svc.check_single_uid_profile("8"), timeout=1.0
    )
    assert (uid_die, out_die, name_die, avt_die) == ("8", CheckOutcome.DIE, None, None)
    assert cancelled_uids == ["8"]


@pytest.mark.asyncio
async def test_bulk_router_handlers_check_recheck_cancel_txt_and_smart_text() -> None:
    from unittest.mock import MagicMock

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.filters import CommandObject
    from aiogram.methods import SendPhoto
    from aiogram.types import Message

    from src.modules.bulk.handlers import (
        CancelBulkCallback,
        RecheckSingleCallback,
        create_bulk_router,
    )

    checker = _StubChecker({"4": CheckOutcome.LIVE, "8": CheckOutcome.DIE}, delay=0.05)
    svc = BulkCheckService(checker=checker, concurrency=5)
    router = create_bulk_router(svc)

    msg_handlers = {h.callback.__name__: h.callback for h in router.message.handlers}
    cb_handlers = {h.callback.__name__: h.callback for h in router.callback_query.handlers}

    def _make_msg(text: str = "", chat_id: int = 100) -> MagicMock:
        m = MagicMock(spec=Message)
        m.chat = MagicMock(id=chat_id)
        m.text = text
        m.photo = None
        m.bot = AsyncMock(spec=Bot)
        m.answer = AsyncMock(return_value=MagicMock(message_id=99, delete=AsyncMock()))
        m.answer_photo = AsyncMock(return_value=MagicMock(message_id=100))
        m.edit_text = AsyncMock()
        m.edit_caption = AsyncMock()
        m.edit_reply_markup = AsyncMock()
        return m

    state = MagicMock()
    state.clear = AsyncMock()
    state.get_state = AsyncMock(return_value=None)

    # 1. /check with no args shows instructions
    m_empty = _make_msg()
    await msg_handlers["cmd_check"](
        m_empty, CommandObject(prefix="/", command="check", args=""), state
    )
    state.clear.assert_awaited_once()
    assert "Kiểm tra nhanh" in m_empty.answer.call_args.args[0]

    # 2. /check with LIVE UID sends photo; if photo fails with TelegramBadRequest, falls back to text
    m_live = _make_msg()
    await msg_handlers["cmd_check"](
        m_live, CommandObject(prefix="/", command="check", args="4"), state
    )
    m_live.answer_photo.assert_awaited_once()

    m_fallback = _make_msg()
    m_fallback.answer_photo.side_effect = TelegramBadRequest(
        method=SendPhoto(chat_id=100, photo="u"), message="bad photo"
    )
    await msg_handlers["cmd_check"](
        m_fallback, CommandObject(prefix="/", command="check", args="4"), state
    )
    m_fallback.answer.assert_awaited_once()

    # 3. /check with invalid UID replies error
    m_bad = _make_msg()
    await msg_handlers["cmd_check"](
        m_bad, CommandObject(prefix="/", command="check", args="abc"), state
    )
    assert "❌" in m_bad.answer.call_args.args[0]

    # 4. cb_recheck_single edits caption on photo message and text on plain message
    q_photo = MagicMock()
    q_photo.message = _make_msg()
    q_photo.message.photo = [MagicMock()]
    q_photo.answer = AsyncMock()
    await cb_handlers["cb_recheck_single"](q_photo, RecheckSingleCallback(uid="4"))
    q_photo.message.edit_caption.assert_awaited_once()
    q_photo.answer.assert_awaited_once()

    q_text = MagicMock()
    q_text.message = _make_msg()
    q_text.answer = AsyncMock()
    await cb_handlers["cb_recheck_single"](q_text, RecheckSingleCallback(uid="8"))
    q_text.message.edit_text.assert_awaited_once()

    # 5. handle_smart_plain_text: ignores plain text, checks 1 UID, starts bulk on 2+ UIDs
    m_chat = _make_msg("hello group chat")
    await msg_handlers["handle_smart_plain_text"](m_chat, state)
    m_chat.answer.assert_not_awaited()

    m_multi = _make_msg("4\n8\n", chat_id=200)
    state.get_state = AsyncMock(return_value="ListLookupStates:waiting_uid")
    await msg_handlers["handle_smart_plain_text"](m_multi, state)
    assert svc.is_chat_busy(200) is True

    # 6. cb_cancel_bulk cancels active job and handles already-finished job
    cancel_markup = m_multi.answer.call_args.kwargs["reply_markup"]
    active_job_id = CancelBulkCallback.unpack(
        cancel_markup.inline_keyboard[0][0].callback_data
    ).job_id
    q_cancel = MagicMock()
    q_cancel.message = _make_msg(chat_id=200)
    q_cancel.answer = AsyncMock()
    await cb_handlers["cb_cancel_bulk"](q_cancel, CancelBulkCallback(job_id=active_job_id))
    assert svc.is_chat_busy(200) is False
    q_cancel.message.edit_text.assert_awaited_once()

    q_cancel_again = MagicMock()
    q_cancel_again.message = _make_msg(chat_id=200)
    q_cancel_again.answer = AsyncMock()
    await cb_handlers["cb_cancel_bulk"](q_cancel_again, CancelBulkCallback(job_id=active_job_id))
    q_cancel_again.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)

    # 7. handle_txt_document: ignores non-.txt, rejects >10MB, processes valid .txt (>64KB to_thread)
    m_doc_skip = _make_msg()
    m_doc_skip.document = MagicMock(file_name="photo.png", file_size=100)
    await msg_handlers["handle_txt_document"](m_doc_skip, m_doc_skip.bot, state)
    m_doc_skip.answer.assert_not_awaited()

    m_doc_big = _make_msg()
    m_doc_big.document = MagicMock(file_name="huge.txt", file_size=11 * 1024 * 1024)
    await msg_handlers["handle_txt_document"](m_doc_big, m_doc_big.bot, state)
    assert "quá lớn" in m_doc_big.answer.call_args.args[0]

    m_doc_ok = _make_msg(chat_id=300)
    m_doc_ok.document = MagicMock(file_name="uids.txt", file_size=70000)

    async def _fake_download(_doc: object, destination: object) -> None:
        import io

        assert isinstance(destination, io.BytesIO)
        payload = ("\n".join(str(10000000000 + i) for i in range(6000)) + "\n").encode("utf-8")
        destination.write(payload)

    m_doc_ok.bot.download = AsyncMock(side_effect=_fake_download)
    await msg_handlers["handle_txt_document"](m_doc_ok, m_doc_ok.bot, state)
    assert svc.is_chat_busy(300) is True
    await svc.close()
