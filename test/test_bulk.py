from __future__ import annotations

import asyncio
from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.types import Message

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
from src.modules.bulk.handlers import (
    CancelBulkCallback,
    TelegramBulkNotifier,
    build_single_check_markup,
    create_bulk_router,
)
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

    async def fetch_avatar_bytes(self, url: str) -> bytes | None:
        return b"\xff\xd8\xff" if url else None

    async def resolve_uid_from_url(self, url: str) -> str | None:
        return "4" if "zuck" in url.lower() else None

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
        ("https://www.facebook.com/4", "4"),
        ("https://fb.com/100004030621748/", "100004030621748"),
        ("https://www.facebook.com/people/Vui-Nguyen/100004030621748/", "100004030621748"),
    ],
)
def test_extract_uid_edge_cases(raw: str, expected: str | None) -> None:
    assert extract_uid(raw) == expected


def test_extract_uids_deduplicates_caps_and_formats() -> None:
    lines = [str(1000000000 + i) for i in range(10000)]
    lines.extend(["1000000000", "invalid_row", "https://fb.com/profile.php?id=1000000001"])
    uids, skipped, truncated = extract_uids_from_text("\n".join(lines))
    assert len(uids) == 10000
    assert skipped == 3
    assert truncated == 0

    # Embedded numeric token extraction ignores leading-zero tokens + respects max_uids cap
    mixed = "\n\n00000\n012345\nuser 100011111111111 note\n100022222222222\n100033333333333\n"
    capped, cap_skipped, cap_truncated = extract_uids_from_text(mixed, max_uids=2)
    assert capped == ["100011111111111", "100022222222222"]
    assert cap_skipped == 2
    assert cap_truncated == 1

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
    assert [b.text for b in live_kb.inline_keyboard[0]] == ["🔗 Mở Facebook", "🔄 Check lại"]
    die_kb = build_single_check_markup("4", CheckOutcome.DIE)
    assert [b.text for b in die_kb.inline_keyboard[0]] == ["➕ Lên kèo UID này", "🔄 Check lại"]
    err_kb = build_single_check_markup("4", CheckOutcome.ERROR)
    assert [b.text for b in err_kb.inline_keyboard[0]] == ["🔄 Check lại"]
    assert live_kb.inline_keyboard[0][0].url == "https://www.facebook.com/profile.php?id=4"

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

    with pytest.raises(ValueError, match="Không tìm thấy UID hợp lệ"):
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

    # Both same chat and different chat can run concurrent bulk jobs cleanly
    same_chat_notifier = _StubBulkNotifier()
    svc.start_bulk_text(
        chat_id=555,
        text="100001\n100002\n",
        status_message_id=78,
        notifier=same_chat_notifier,
    )
    svc.start_bulk_text(
        chat_id=666,
        text="100001\n100003\n",
        status_message_id=79,
        notifier=other_notifier,
    )

    await asyncio.wait_for(notifier.done.wait(), timeout=5.0)
    await asyncio.wait_for(same_chat_notifier.done.wait(), timeout=5.0)
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

    with pytest.raises(ValueError, match="Không tìm thấy UID hợp lệ"):
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
        m.chat = MagicMock(id=chat_id, type="private")
        m.from_user = MagicMock(id=1)
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

    # 2. /check with LIVE UID uploads BufferedInputFile directly; if photo send fails, falls back to text
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

    # 4. cb_recheck_single updates media on photo message when LIVE and edits text on plain message
    q_photo = MagicMock()
    q_photo.message = _make_msg()
    q_photo.message.photo = [MagicMock()]
    q_photo.message.edit_media = AsyncMock()
    q_photo.answer = AsyncMock()
    await cb_handlers["cb_recheck_single"](q_photo, RecheckSingleCallback(uid="4"))
    q_photo.message.edit_media.assert_awaited_once()
    q_photo.answer.assert_awaited_once()

    q_text = MagicMock()
    q_text.message = _make_msg()
    q_text.answer = AsyncMock()
    await cb_handlers["cb_recheck_single"](q_text, RecheckSingleCallback(uid="8"))
    q_text.message.edit_text.assert_awaited_once()

    # 4b. cb_recheck_single upgrades text-only message to photo when UID becomes LIVE with avatar
    q_upgrade = MagicMock()
    q_upgrade.message = _make_msg()
    q_upgrade.message.delete = AsyncMock()
    q_upgrade.answer = AsyncMock()
    await cb_handlers["cb_recheck_single"](q_upgrade, RecheckSingleCallback(uid="4"))
    q_upgrade.message.answer_photo.assert_awaited_once()
    q_upgrade.message.delete.assert_awaited_once()

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
    q_cancel.from_user = MagicMock(id=1)
    q_cancel.answer = AsyncMock()
    await cb_handlers["cb_cancel_bulk"](q_cancel, CancelBulkCallback(job_id=active_job_id))
    assert svc.is_chat_busy(200) is False
    q_cancel.message.edit_text.assert_awaited_once()

    q_cancel_again = MagicMock()
    q_cancel_again.message = _make_msg(chat_id=200)
    q_cancel_again.from_user = MagicMock(id=1)
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


@pytest.mark.asyncio
async def test_bulk_edge_cases_cancel_all_errors_small_txt_and_smart_text() -> None:
    from unittest.mock import MagicMock

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText, SendPhoto
    from aiogram.types import Message

    from src.modules.bulk.handlers import (
        CancelBulkCallback,
        RecheckSingleCallback,
        create_bulk_router,
    )

    # 1. cancel_bulk_job(chat_id, job_id=None) cancels all jobs for that chat
    slow_svc = BulkCheckService(checker=_StubChecker({}, delay=5.0), concurrency=2)
    assert slow_svc.cancel_bulk_job(700) is False
    slow_svc.start_bulk_text(
        chat_id=700, text="10001\n10002\n", status_message_id=1, notifier=_StubBulkNotifier()
    )
    slow_svc.start_bulk_text(
        chat_id=700, text="10003\n10004\n", status_message_id=2, notifier=_StubBulkNotifier()
    )
    assert slow_svc.is_chat_busy(700) is True
    assert slow_svc.cancel_bulk_job(700) is True
    assert slow_svc.is_chat_busy(700) is False
    await slow_svc.close()

    # 2. check_single_uid_profile cancels profile_task if check() raises unexpectedly
    cancelled_uids: list[str] = []

    class _BoomChecker(_StubChecker):
        async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                cancelled_uids.append(uid)
                raise
            return None, None

    boom_svc = BulkCheckService(checker=_BoomChecker({}, fail_uid="4", delay=0.01))
    with pytest.raises(RuntimeError, match="boom"):
        await boom_svc.check_single_uid_profile("4")
    assert cancelled_uids == ["4"]

    # 3. TelegramBulkNotifier.update_progress swallows TelegramBadRequest and generic errors
    bot_err = AsyncMock(spec=Bot)
    bot_err.edit_message_text.side_effect = [
        TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=2, text="x"), message="not modified"
        ),
        RuntimeError("network blip"),
    ]
    notifier_err = TelegramBulkNotifier(bot_err, job_id="j_err")
    await notifier_err.update_progress(1, 2, "p1")
    await notifier_err.update_progress(1, 2, "p2")
    assert bot_err.edit_message_text.await_count == 2

    # 4. Router edge cases: non-Message callbacks, invalid UID recheck, photo fail on upgrade,
    #    small .txt, bad .txt, document=None, single-line & 1-UID multi-line smart text, bot=None
    checker = _StubChecker({"4": CheckOutcome.LIVE, "8": CheckOutcome.DIE})
    svc = BulkCheckService(checker=checker, concurrency=4)
    router = create_bulk_router(svc)
    msg_handlers = {h.callback.__name__: h.callback for h in router.message.handlers}
    cb_handlers = {h.callback.__name__: h.callback for h in router.callback_query.handlers}

    def _make_msg(text: str = "", chat_id: int = 400) -> MagicMock:
        m = MagicMock(spec=Message)
        m.chat = MagicMock(id=chat_id, type="private")
        m.from_user = MagicMock(id=1)
        m.text = text
        m.photo = None
        m.document = None
        m.bot = AsyncMock(spec=Bot)
        m.answer = AsyncMock(return_value=MagicMock(message_id=50))
        m.answer_photo = AsyncMock(return_value=MagicMock(message_id=51))
        m.edit_text = AsyncMock()
        m.edit_caption = AsyncMock()
        m.delete = AsyncMock()
        return m

    state = MagicMock()
    state.clear = AsyncMock()
    state.get_state = AsyncMock(return_value="some_state")

    # Non-Message callbacks just answer query
    q_none = MagicMock(message=None, answer=AsyncMock())
    await cb_handlers["cb_recheck_single"](q_none, RecheckSingleCallback(uid="4"))
    await cb_handlers["cb_cancel_bulk"](q_none, CancelBulkCallback(job_id="j1"))
    assert q_none.answer.await_count == 2

    # Invalid UID in recheck shows alert
    q_bad_uid = MagicMock(message=_make_msg(), answer=AsyncMock())
    await cb_handlers["cb_recheck_single"](q_bad_uid, RecheckSingleCallback(uid="not-a-uid"))
    q_bad_uid.answer.assert_awaited_once()
    assert q_bad_uid.answer.call_args.kwargs.get("show_alert") is True

    # Recheck upgrade falls back to edit_text when photo upload fails
    q_up_fail = MagicMock(message=_make_msg(), answer=AsyncMock())
    q_up_fail.message.answer_photo.side_effect = TelegramBadRequest(
        method=SendPhoto(chat_id=400, photo="u"), message="upload failed"
    )
    await cb_handlers["cb_recheck_single"](q_up_fail, RecheckSingleCallback(uid="4"))
    q_up_fail.message.edit_text.assert_awaited_once()
    q_up_fail.message.delete.assert_not_awaited()

    # handle_txt_document: document=None, small valid .txt (<64KB) clearing state, and invalid .txt
    m_nodoc = _make_msg()
    await msg_handlers["handle_txt_document"](m_nodoc, m_nodoc.bot, state)
    m_nodoc.answer.assert_not_awaited()

    async def _dl_small(_doc: object, destination: object) -> None:
        import io

        assert isinstance(destination, io.BytesIO)
        destination.write(b"4\n8\n")

    m_small_txt = _make_msg(chat_id=401)
    m_small_txt.document = MagicMock(file_name="small.txt", file_size=10)
    m_small_txt.bot.download = AsyncMock(side_effect=_dl_small)
    await msg_handlers["handle_txt_document"](m_small_txt, m_small_txt.bot, state)
    state.clear.assert_awaited_once()

    async def _dl_bad(_doc: object, destination: object) -> None:
        import io

        assert isinstance(destination, io.BytesIO)
        destination.write(b"no uids at all\n")

    m_bad_txt = _make_msg(chat_id=402)
    m_bad_txt.document = MagicMock(file_name="bad.txt", file_size=20)
    m_bad_txt.bot.download = AsyncMock(side_effect=_dl_bad)
    await msg_handlers["handle_txt_document"](m_bad_txt, m_bad_txt.bot, state)
    assert "❌" in m_bad_txt.answer.call_args.args[0]

    # handle_smart_plain_text: empty, single UID, multi-line invalid, multi-line 1 unique UID
    m_blank = _make_msg("   ")
    await msg_handlers["handle_smart_plain_text"](m_blank, state)
    m_blank.answer.assert_not_awaited()

    m_single = _make_msg("4")
    await msg_handlers["handle_smart_plain_text"](m_single, state)
    m_single.answer_photo.assert_awaited_once()

    m_bad_multi = _make_msg("hello\nworld")
    await msg_handlers["handle_smart_plain_text"](m_bad_multi, state)
    m_bad_multi.answer.assert_not_awaited()

    m_dedup_one = _make_msg("4\n4\n")
    await msg_handlers["handle_smart_plain_text"](m_dedup_one, state)
    m_dedup_one.answer_photo.assert_awaited_once()

    # Avatar bytes returning None falls back to plain text
    checker.fetch_avatar_bytes = AsyncMock(return_value=None)
    m_no_bytes = _make_msg("4")
    await msg_handlers["handle_smart_plain_text"](m_no_bytes, state)
    m_no_bytes.answer_photo.assert_not_awaited()
    m_no_bytes.answer.assert_awaited_once()

    # Pasting a Facebook username/vanity URL resolves UID and checks immediately
    from src.modules.bulk.domain import extract_fb_url

    assert extract_fb_url("https://www.facebook.com/vuinguyen.2026") == (
        "https://www.facebook.com/vuinguyen.2026"
    )
    assert extract_fb_url("fb.com/zuck") == "https://fb.com/zuck"
    assert extract_fb_url("https://www.facebook.com/groups/12345") is None

    m_vanity = _make_msg("https://www.facebook.com/zuck")
    await msg_handlers["handle_smart_plain_text"](m_vanity, state)
    m_vanity.answer.assert_awaited_once()

    # Unresolvable Facebook share/vanity link gives clear DIE/private guidance
    m_dead_share = _make_msg("https://www.facebook.com/share/1FDDT4Su9K/?mibextid=wwXIfr")
    await msg_handlers["handle_smart_plain_text"](m_dead_share, state)
    assert "tài khoản có thể đã DIE" in m_dead_share.answer.call_args.args[0]

    # cb_recheck_single on a LIVE photo message that turns DIE replaces it with plain text
    q_to_die = MagicMock()
    q_to_die.message = _make_msg()
    q_to_die.message.photo = [MagicMock()]
    q_to_die.message.delete = AsyncMock()
    q_to_die.answer = AsyncMock()
    await cb_handlers["cb_recheck_single"](q_to_die, RecheckSingleCallback(uid="8"))
    q_to_die.message.answer.assert_awaited_once()
    q_to_die.message.delete.assert_awaited_once()

    await svc.close()


@pytest.mark.asyncio
async def test_bulk_hardening_busy_guard_truncation_and_group_auth() -> None:
    from src.modules.bulk.domain import BulkSummary, format_bulk_summary

    summary_text = format_bulk_summary(
        BulkSummary(
            job_id="abc",
            total=2,
            live=1,
            die=1,
            error=0,
            elapsed_seconds=1.2,
            truncated=5,
        )
    )
    assert "Đã cắt bớt" in summary_text
    assert "5" in summary_text

    gate = asyncio.Event()

    class _SlowChecker:
        async def check(self, uid: str) -> UidCheckResult:
            await gate.wait()
            return UidCheckResult(uid=uid, outcome=CheckOutcome.LIVE)

        async def verify_live(self, uid: str) -> UidCheckResult:
            return UidCheckResult(uid=uid, outcome=CheckOutcome.LIVE)

        async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
            return None, None

        async def fetch_avatar_bytes(self, url: str) -> bytes | None:
            return None

        async def resolve_uid_from_url(self, url: str) -> str | None:
            return None

        async def close(self) -> None:
            return None

    svc = BulkCheckService(checker=_SlowChecker(), concurrency=2, progress_interval_seconds=10.0)
    router = create_bulk_router(svc)
    msg_handlers = {h.callback.__name__: h.callback for h in router.message.handlers}
    cb_handlers = {h.callback.__name__: h.callback for h in router.callback_query.handlers}

    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    state.clear = AsyncMock()

    m_group = MagicMock(spec=Message)
    m_group.chat = MagicMock(id=-100999, type="group")
    m_group.from_user = MagicMock(id=10)
    m_group.text = "4\n5"
    m_group.bot = AsyncMock(spec=Bot)
    m_group.answer = AsyncMock(return_value=MagicMock(message_id=50))
    await msg_handlers["handle_smart_plain_text"](m_group, state)
    assert svc.is_chat_busy(-100999) is True

    # Second bulk request in same busy chat is rejected
    m_busy = MagicMock(spec=Message)
    m_busy.chat = MagicMock(id=-100999, type="group")
    m_busy.from_user = MagicMock(id=10)
    m_busy.text = "6\n7"
    m_busy.bot = AsyncMock(spec=Bot)
    m_busy.answer = AsyncMock()
    await msg_handlers["handle_smart_plain_text"](m_busy, state)
    assert "đang có phiên kiểm tra hàng loạt" in m_busy.answer.call_args.args[0]

    active_job_id = next(iter(svc._jobs))  # type: ignore[reportPrivateUsage]

    # Non-owner non-admin in group cannot cancel
    q_stranger = MagicMock()
    q_stranger.from_user = MagicMock(id=99)
    q_stranger.message = MagicMock(spec=Message)
    q_stranger.message.chat = MagicMock(id=-100999, type="group")
    q_stranger.message.bot = AsyncMock(spec=Bot)
    q_stranger.message.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
    q_stranger.answer = AsyncMock()
    await cb_handlers["cb_cancel_bulk"](q_stranger, CancelBulkCallback(job_id=active_job_id))
    assert q_stranger.answer.call_args.kwargs.get("show_alert") is True
    assert svc.is_chat_busy(-100999) is True

    # Group admin can cancel
    q_admin = MagicMock()
    q_admin.from_user = MagicMock(id=88)
    q_admin.message = MagicMock(spec=Message)
    q_admin.message.chat = MagicMock(id=-100999, type="group")
    q_admin.message.bot = AsyncMock(spec=Bot)
    q_admin.message.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
    q_admin.message.edit_text = AsyncMock()
    q_admin.answer = AsyncMock()
    await cb_handlers["cb_cancel_bulk"](q_admin, CancelBulkCallback(job_id=active_job_id))
    assert svc.is_chat_busy(-100999) is False

    gate.set()
    await svc.close()


@pytest.mark.asyncio
async def test_strict_bulk_worker_crash_isolation_cancellation_and_adversarial_parsing() -> None:
    from src.modules.bulk.domain import extract_fb_url, extract_uids_from_text

    # 1. Adversarial UID & URL extraction: BOM, CRLF, leading zeros, 21-digit, non-profile paths
    raw = "\ufeff012345\r\n123456789012345678901\r\nuser: 100001234567890 (note)\r\n4\r\n4\r\n"
    uids, skipped, truncated = extract_uids_from_text(raw, max_uids=10)
    assert uids == ["100001234567890", "4"]
    assert skipped == 3  # 012345, 21-digit, duplicate "4"
    assert truncated == 0

    uids_cap, _, trunc_cap = extract_uids_from_text("10001\n10002\n10003\n10004\n", max_uids=2)
    assert uids_cap == ["10001", "10002"]
    assert trunc_cap == 2

    for bad_url in (
        "https://facebook.com/login/identify",
        "https://www.facebook.com/reel/12345",
        "https://fb.com/sharer.php?u=x",
        "https://facebook.com/zuck extra",
        "https://facebook.com/zuck\n4",
        "https://example.com/zuck",
    ):
        assert extract_fb_url(bad_url) is None

    # 2. Worker exception inside start_bulk_uids cancels sibling workers, notifies user, releases busy lock
    sibling_cancelled = asyncio.Event()

    class _CrashingChecker:
        async def check(self, uid: str) -> UidCheckResult:
            if uid == "10001":
                await asyncio.sleep(0.01)
                raise RuntimeError("fatal worker crash")
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
            return UidCheckResult(uid=uid, outcome=CheckOutcome.LIVE)

        async def verify_live(self, uid: str) -> UidCheckResult:
            return UidCheckResult(uid=uid, outcome=CheckOutcome.LIVE)

        async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
            return None, None

        async def fetch_avatar_bytes(self, url: str) -> bytes | None:
            return None

        async def resolve_uid_from_url(self, url: str) -> str | None:
            return None

        async def close(self) -> None:
            return None

    notifier = _StubBulkNotifier()
    svc = BulkCheckService(checker=_CrashingChecker(), concurrency=2, progress_interval_seconds=0.5)
    job_id, total, _ = svc.start_bulk_text(
        chat_id=900,
        text="10001\n10002\n",
        status_message_id=77,
        notifier=notifier,
    )
    assert total == 2
    assert svc.is_chat_busy(900) is True
    await asyncio.wait_for(sibling_cancelled.wait(), timeout=2.0)
    # Wait for background runner cleanup
    for _ in range(50):
        if not svc.is_chat_busy(900):
            break
        await asyncio.sleep(0.01)
    assert svc.is_chat_busy(900) is False
    assert any(job_id in msg and "gặp sự cố" in msg for msg in notifier.progress_updates)
    await svc.close()
