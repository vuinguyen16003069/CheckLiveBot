from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from aiogram.types import Message

from src.core.ports import CheckOutcome, UidCheckResult
from src.core.redis import RedisLock
from src.modules.watch.domain import (
    WatchItem,
    WatchStats,
    WatchStatus,
    format_duration,
    format_live_notification,
    format_watch_card,
    format_watch_list,
    format_watch_stats,
    format_welcome_dashboard,
    parse_add_line,
)
from src.modules.watch.handlers import create_watch_router
from src.modules.watch.service import (
    DuplicateWatchError,
    RedisWatchRepository,
    WatcherScanService,
    WatchService,
    WatchTtlPolicy,
)
from src.modules.watch.ui import (
    DealDraft,
    WatchActionCallback,
    build_delete_confirm_keyboard,
    build_list_keyboard,
    build_reply_keyboard,
    build_watch_keyboard,
    build_wizard_keyboard,
    format_wizard_step,
)


class _StubChecker:
    def __init__(
        self,
        mapping: dict[str, CheckOutcome],
        verify_mapping: dict[str, tuple[CheckOutcome, str | None]] | None = None,
    ) -> None:
        self.mapping = mapping
        self.verify_mapping = verify_mapping or {}
        self.check_calls: list[str] = []

    async def check(self, uid: str) -> UidCheckResult:
        self.check_calls.append(uid)
        return UidCheckResult(uid=uid, outcome=self.mapping.get(uid, CheckOutcome.DIE))

    async def verify_live(self, uid: str) -> UidCheckResult:
        if uid in self.verify_mapping:
            outcome, name = self.verify_mapping[uid]
            return UidCheckResult(uid=uid, outcome=outcome, detail=name)
        return UidCheckResult(
            uid=uid,
            outcome=self.mapping.get(uid, CheckOutcome.DIE),
            detail="Trang Linh",
        )

    async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
        if self.mapping.get(uid) == CheckOutcome.LIVE:
            return "Trang Linh", "https://scontent.xx.fbcdn.net/avatar.jpg"
        return None, None

    async def fetch_avatar_bytes(self, url: str) -> bytes | None:
        return b"\xff\xd8\xff" if url else None

    async def resolve_uid_from_url(self, url: str) -> str | None:
        return "100004030621748" if "vuinguyen" in url.lower() else None

    async def close(self) -> None:
        return None


class _StubNotifier:
    def __init__(self) -> None:
        self.notified: list[WatchItem] = []

    async def notify_live(self, item: WatchItem) -> None:
        self.notified.append(item)


def test_parse_add_line_duration_and_card_formatting() -> None:
    parsed = parse_add_line(
        "https://facebook.com/profile.php?id=100012345678901 | <VIP> & Co | 150000 | 282"
    )
    assert (parsed.uid, parsed.note, parsed.price, parsed.faq) == (
        "100012345678901",
        "<VIP> & Co",
        "150000",
        "282",
    )

    with pytest.raises(ValueError, match="Thiếu dữ liệu"):
        parse_add_line("   ")
    with pytest.raises(ValueError, match="UID không hợp lệ"):
        parse_add_line("invalid_line")

    bare = parse_add_line("4")
    assert (bare.uid, bare.note, bare.price, bare.faq) == ("4", "Không có ghi chú", "0", "")

    # Duration formatting across units + negative clamp
    t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    assert format_duration(t0, t0 - timedelta(seconds=10)) == "0 giây"
    assert format_duration(t0, t0 + timedelta(seconds=42)) == "42 giây"
    assert format_duration(t0, t0 + timedelta(hours=2, minutes=15)) == "2 giờ 15 phút"
    assert format_duration(t0, t0 + timedelta(days=3, hours=4, minutes=5)) == "3 ngày 4 giờ 5 phút"

    # Card formatting across all statuses, price formats, naive datetime & spoiler safety
    naive_created = datetime(2026, 1, 1, 10, 0, 0)  # noqa: DTZ001
    item = WatchItem(
        id=1,
        chat_id=10,
        owner_user_id=20,
        uid=parsed.uid,
        note=parsed.note,
        price=parsed.price,
        faq=parsed.faq,
        created_at=naive_created,
        last_checked_at=naive_created,
    )
    card = format_watch_card(item)
    assert "&lt;VIP&gt; &amp; Co" in card
    assert "150.000đ" in card
    assert "<code>100012345678901</code>" in card

    # Hidden mode must NEVER nest <code> inside <tg-spoiler>
    item.is_hidden = True
    hidden_card = format_watch_card(item)
    assert "<tg-spoiler>100012345678901</tg-spoiler>" in hidden_card
    assert "<tg-spoiler><code>" not in hidden_card
    assert "<b>Thời gian:</b> <tg-spoiler>" in hidden_card

    # LIVE notification includes FAQ → LIVE and elapsed duration
    live_item = item.model_copy(
        update={
            "status": WatchStatus.LIVE,
            "is_hidden": False,
            "price": "",
            "created_at": t0,
            "live_detected_at": t0 + timedelta(minutes=30),
        }
    )
    live_text = format_live_notification(live_item)
    assert "THÔNG BÁO TÀI KHOẢN ĐÃ LIVE!" in live_text
    assert "282 → LIVE ✅" in live_text
    assert "30 phút" in live_text
    assert "0đ" in live_text
    # Always displays created_at (10:00 UTC = 17:00:00 VN), NOT live_detected_at (17:30:00)
    assert "17:00:00 01/01/2026" in live_text

    done_card = format_watch_card(
        item.model_copy(update={"status": WatchStatus.DONE, "price": "500"})
    )
    assert "DONE" in done_card
    assert "500đ" in done_card

    cancel_card = format_watch_card(item.model_copy(update={"status": WatchStatus.CANCELLED}))
    assert "ĐÃ HỦY" in cancel_card


def test_ui_keyboards_list_formats_and_wizard_recovery() -> None:
    stats = WatchStats(total=10, die=4, live=2, done=4, cancelled=0)
    assert "Đang chờ: <b>4</b>" in format_welcome_dashboard(stats)
    assert "Tổng số kèo: <b>10</b>" in format_watch_stats(stats)

    rk = build_reply_keyboard()
    assert len(rk.keyboard) == 2
    assert len(rk.keyboard[0]) == 2

    draft = DealDraft()
    assert "Bước 1/4" in format_wizard_step(1, draft)
    assert len(build_wizard_keyboard(step=1).inline_keyboard[0]) == 1

    kb3 = build_wizard_keyboard(step=3)
    assert any(btn.text == "50k" for btn in kb3.inline_keyboard[0])
    assert any("Quay lại" in btn.text for btn in kb3.inline_keyboard[1])

    kb4 = build_wizard_keyboard(step=4)
    assert [btn.text for btn in kb4.inline_keyboard[0]] == ["282", "956", "583"]

    sample_item = WatchItem(id=7, chat_id=1, owner_user_id=1, uid="10001", note="N", price="0")
    list_kb = build_list_keyboard("ALL", 2, 3)
    flat_texts = [btn.text for row in list_kb.inline_keyboard for btn in row]
    assert "🔄 Quét lại DIE" not in flat_texts
    assert "◀️" in flat_texts
    assert "▶️" in flat_texts
    assert "📄 2/3" in flat_texts

    empty_list = format_watch_list([], status_filter="DIE", page=1, total_pages=1, total_items=0)
    assert "Chưa có kèo nào phù hợp" in empty_list

    list_no_bot = format_watch_list(
        [sample_item],
        status_filter="ALL",
        page=1,
        total_pages=1,
        total_items=1,
        bot_username=None,
    )
    assert "<code>10001</code>" in list_no_bot

    list_html = format_watch_list(
        [
            sample_item.model_copy(
                update={
                    "note": "VIA CỔ 1000X - " + "a" * 120,
                    "faq": "583 toàn vẹn",
                }
            )
        ],
        status_filter="ALL",
        page=1,
        total_pages=1,
        total_items=1,
        bot_username="my_checklive_bot",
    )
    assert 'href="https://t.me/my_checklive_bot?start=w_7"' in list_html
    assert "<b>0đ</b>" in list_html
    assert "[583 toàn vẹn]" in list_html
    assert "…" in list_html
    assert "⇨ Chạm UID để mở chi tiết" in list_html

    list_email = format_watch_list(
        [sample_item.model_copy(update={"note": "abc@gmail.com"})],
        status_filter="ALL",
        page=1,
        total_pages=1,
        total_items=1,
        bot_username="my_checklive_bot",
    )
    assert "abc@\u2060gmail.\u2060com" in list_email

    del_kb = build_delete_confirm_keyboard(7)
    assert any("Xác nhận xóa #7" in btn.text for btn in del_kb.inline_keyboard[0])

    die_kb = build_watch_keyboard(sample_item)
    assert [btn.text for btn in die_kb.inline_keyboard[0]] == [
        "🙈 Ẩn thông tin",
        "🐵 Hiện thông tin",
    ]
    assert [btn.text for btn in die_kb.inline_keyboard[1]] == ["❌ Hủy kèo", "🗑 Xóa"]
    assert [btn.text for btn in die_kb.inline_keyboard[2]] == ["✏️ Sửa", "🔙 Về danh sách"]

    live_item = sample_item.model_copy(update={"status": WatchStatus.LIVE})
    live_kb = build_watch_keyboard(live_item)
    live_texts = [btn.text for row in live_kb.inline_keyboard for btn in row]
    assert "✏️ Sửa" in live_texts
    assert "🔄 Tiếp tục theo dõi" in live_texts

    edit_draft = DealDraft(uid="10001", edit_watch_id=7)
    assert "SỬA KÈO #7" in format_wizard_step(2, edit_draft)
    assert "Bước 1/3" in format_wizard_step(2, edit_draft)
    assert len(build_wizard_keyboard(step=2, min_step=2).inline_keyboard[0]) == 1


@pytest.mark.asyncio
async def test_watch_duplicate_rejection_cap_and_delete() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker(
        {"100099": CheckOutcome.DIE, "100100": CheckOutcome.DIE, "20531316728": CheckOutcome.LIVE}
    )
    svc = WatchService(repo=repo, checker=checker, max_active_per_chat=1)

    item1 = await svc.add_watch(100, 1, "100099 | Khách cũ | 10k")
    with pytest.raises(DuplicateWatchError) as exc_info:
        await svc.add_watch(100, 1, "100099 | Khách mới | 50k")
    assert exc_info.value.existing.id == item1.id

    # Active DIE cap enforced at 1
    with pytest.raises(ValueError, match="đạt giới hạn 1 kèo DIE"):
        await svc.add_watch(100, 1, "100100 | Bị chặn cap | 10k")

    # Deleting frees the cap and unlocks UID for re-adding
    await svc.delete_watch(100, item1.id)
    re_added = await svc.add_watch(100, 1, "100099 | Lên lại | 50k")
    assert re_added.uid == "100099"
    await redis.aclose()


@pytest.mark.asyncio
async def test_watch_service_lifecycle_pagination_recheck_and_isolation() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker(
        {"100001": CheckOutcome.DIE, "100002": CheckOutcome.LIVE, "100003": CheckOutcome.DIE}
    )
    svc = WatchService(repo=repo, checker=checker, max_active_per_chat=50)

    w1 = await svc.add_watch(chat_id=100, user_id=1, raw_args="100001 | Khách 1 | 50k")
    w2 = await svc.add_watch(chat_id=100, user_id=1, raw_args="100002 | Khách 2 | 80k")
    await svc.save_avatar_file_id(w2, "AgACAgUAAxkBAA_cached_file_id")
    reloaded_w2 = await svc.get_watch(chat_id=100, watch_id=w2.id)
    assert reloaded_w2.avatar_file_id == "AgACAgUAAxkBAA_cached_file_id"
    w3 = await svc.add_watch(chat_id=100, user_id=1, raw_args="100003 | Khách 3 | 90k")
    assert (w1.status, w2.status, w3.status) == (
        WatchStatus.DIE,
        WatchStatus.LIVE,
        WatchStatus.DIE,
    )

    # Cross-chat isolation on mark_status, update_watch, delete_watch
    with pytest.raises(ValueError, match="Không tìm thấy kèo"):
        await svc.mark_status(chat_id=200, watch_id=w1.id, new_status=WatchStatus.DONE)
    with pytest.raises(ValueError, match="Không tìm thấy kèo"):
        await svc.update_watch(200, w1.id, note="Hack", price="0", faq="")

    # Global per-chat hide preference persists across all watches in chat 100 (365-day TTL)
    w1_hidden = await svc.set_hidden(chat_id=100, watch_id=w1.id, hidden=True)
    assert w1_hidden.is_hidden is True
    ttl_pref = await redis.ttl("test:pref:hidden:100")
    assert 364 * 86400 < ttl_pref <= 365 * 86400
    assert (await svc.get_watch(chat_id=100, watch_id=w2.id)).is_hidden is True

    # update_watch preserves is_hidden and normalizes blanks / "-"
    w1_edited = await svc.update_watch(100, w1.id, note="Khách VIP mới", price="200k", faq="956")
    assert (w1_edited.note, w1_edited.price, w1_edited.faq, w1_edited.is_hidden) == (
        "Khách VIP mới",
        "200k",
        "956",
        True,
    )
    w1_cleared = await svc.update_watch(100, w1.id, note="  ", price="", faq="-")
    assert (w1_cleared.note, w1_cleared.price, w1_cleared.faq) == ("Theo dõi nhanh", "0", "")

    assert (await svc.set_hidden(chat_id=100, watch_id=w1.id, hidden=False)).is_hidden is False
    assert await redis.exists("test:pref:hidden:100") == 0

    # Status transitions: DONE -> DIE -> CANCELLED
    w1_done = await svc.mark_status(chat_id=100, watch_id=w1.id, new_status=WatchStatus.DONE)
    assert w1_done.status == WatchStatus.DONE
    assert w1_done.completed_at is not None

    w1_resumed = await svc.mark_status(chat_id=100, watch_id=w1.id, new_status=WatchStatus.DIE)
    assert w1_resumed.status == WatchStatus.DIE
    assert w1_resumed.completed_at is None

    # Multi-page pagination clamping (add 12 more items to chat 300)
    for i in range(12):
        uid = str(200000 + i)
        checker.mapping[uid] = CheckOutcome.DIE
        await svc.add_watch(chat_id=300, user_id=1, raw_args=f"{uid} | Item {i} | 10k")
    page1, total300, pages300 = await svc.list_watches(300, page=1)
    assert (len(page1), total300, pages300) == (10, 12, 2)
    page_clamped, _, _ = await svc.list_watches(300, page=99)
    assert len(page_clamped) == 2

    await redis.aclose()


@pytest.mark.asyncio
async def test_watcher_double_verify_dedupe_and_lock() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    lock = RedisLock(redis, prefix="test")
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker(
        {"100010": CheckOutcome.DIE, "100011": CheckOutcome.DIE, "100012": CheckOutcome.DIE}
    )
    svc = WatchService(repo=repo, checker=checker)
    notifier = _StubNotifier()
    scanner = WatcherScanService(repo=repo, checker=checker, lock=lock, notifier=notifier)

    # Empty scan returns 0 without calling checker
    assert await scanner.scan_once() == 0
    assert checker.check_calls == []

    await svc.add_watch(chat_id=100, user_id=1, raw_args="100010 | Error | 10k")
    w_live_1 = await svc.add_watch(chat_id=100, user_id=1, raw_args="100011 | Real Live | 20k")
    await svc.add_watch(chat_id=200, user_id=2, raw_args="100011 | Same UID Chat 2 | 25k")
    await svc.add_watch(chat_id=100, user_id=1, raw_args="100012 | False Live | 30k")

    # Pre-acquire lock for w_live_1 to verify RedisLock prevents duplicate alert
    assert await lock.acquire_lock(f"notify:{w_live_1.id}", ttl_seconds=60) is True

    checker.mapping["100010"] = CheckOutcome.ERROR
    checker.mapping["100011"] = CheckOutcome.LIVE
    checker.mapping["100012"] = CheckOutcome.LIVE
    checker.verify_mapping["100011"] = (CheckOutcome.LIVE, "Trang Linh")
    checker.verify_mapping["100012"] = (CheckOutcome.DIE, None)

    checker.check_calls.clear()
    count = await scanner.scan_once()
    # Only chat 200 gets notified because chat 100's notify lock was already held
    assert count == 1
    assert checker.check_calls.count("100011") == 1
    assert [n.chat_id for n in notifier.notified] == [200]
    await redis.aclose()


@pytest.mark.asyncio
async def test_tiered_ttl_policy_lazy_prune_reopen_and_chat_uid_compaction() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    policy = WatchTtlPolicy(
        die_days=90,
        live_days=60,
        done_days=30,
        cancelled_days=14,
        pref_days=365,
        seq_days=0,
    )
    repo = RedisWatchRepository(redis, prefix="test", ttl_policy=policy)
    checker = _StubChecker(
        {
            "100001": CheckOutcome.DIE,
            "100002": CheckOutcome.LIVE,
            "100003": CheckOutcome.DIE,
            "100004": CheckOutcome.DIE,
        }
    )
    svc = WatchService(repo=repo, checker=checker)

    w_die = await svc.add_watch(100, 1, "100001 | Active DIE | 10k")
    w_live = await svc.add_watch(100, 1, "100002 | Active LIVE | 20k")
    w_done = await svc.add_watch(100, 1, "100003 | Done Deal | 30k")
    w_done = await svc.mark_status(100, w_done.id, WatchStatus.DONE)
    w_cancel = await svc.add_watch(100, 1, "100004 | Cancelled Deal | 40k")
    w_cancel = await svc.mark_status(100, w_cancel.id, WatchStatus.CANCELLED)
    await svc.set_hidden(100, w_die.id, True)

    # Verify exact tiered TTL per key type
    assert await redis.ttl("test:seq:watch_id") == -1  # permanent
    assert 364 * 86400 < await redis.ttl("test:pref:hidden:100") <= 365 * 86400
    assert 89 * 86400 < await redis.ttl(f"test:watch:{w_die.id}") <= 90 * 86400
    assert 59 * 86400 < await redis.ttl(f"test:watch:{w_live.id}") <= 60 * 86400
    assert 29 * 86400 < await redis.ttl(f"test:watch:{w_done.id}") <= 30 * 86400
    assert 13 * 86400 < await redis.ttl(f"test:watch:{w_cancel.id}") <= 14 * 86400

    # Container keys MUST stay at max watch TTL (90d), never shortened to 14d by CANCELLED
    assert 89 * 86400 < await redis.ttl("test:watches:chat:100") <= 90 * 86400
    assert 89 * 86400 < await redis.ttl("test:chat_uid:100") <= 90 * 86400
    assert 89 * 86400 < await redis.ttl("test:watches:die") <= 90 * 86400
    assert 13 * 86400 < await redis.ttl("test:watches:chat:100:status:CANCELLED") <= 14 * 86400

    # Old DIE (created 85 days ago) turning LIVE gets a fresh 60d from live_detected_at
    w_die.created_at = datetime.now(UTC) - timedelta(days=85)
    w_die.status = WatchStatus.LIVE
    w_die.live_detected_at = datetime.now(UTC)
    await repo.update(w_die, previous_status=WatchStatus.DIE)
    assert 59 * 86400 < await redis.ttl(f"test:watch:{w_die.id}") <= 60 * 86400

    # Reopening an old item back to DIE resets last_checked_at and grants a fresh 90d TTL
    reopened = await svc.mark_status(100, w_die.id, WatchStatus.DIE)
    assert reopened.status == WatchStatus.DIE
    assert 89 * 86400 < await redis.ttl(f"test:watch:{w_die.id}") <= 90 * 86400

    # Simulate w_cancel aged 16 days (>14d) & w_done aged 35 days (>30d), with Redis TTL expired
    ts_16d = (datetime.now(UTC) - timedelta(days=16)).timestamp()
    ts_35d = (datetime.now(UTC) - timedelta(days=35)).timestamp()
    await redis.zadd("test:watches:chat:100:status:CANCELLED", {str(w_cancel.id): ts_16d})
    await redis.zadd("test:watches:chat:100:status:DONE", {str(w_done.id): ts_35d})
    await redis.delete(f"test:watch:{w_cancel.id}", f"test:watch:{w_done.id}")

    # get_stats() prunes per-status cutoffs so total == die + live + done + cancelled
    stats = await svc.get_stats(100)
    assert (stats.total, stats.die, stats.live, stats.done, stats.cancelled) == (2, 1, 1, 0, 0)

    # ALL tab lazily ZREMs items whose shorter status TTL expired before max_watch_days
    items, total, _ = await svc.list_watches(100)
    assert total == 2
    assert {it.id for it in items} == {w_die.id, w_live.id}
    assert await redis.zcard("test:watches:chat:100") == 2

    # Inject >50 dead chat_uid entries -> get_stats() triggers opportunistic compaction
    async with redis.pipeline(transaction=False) as pipe:
        for i in range(60):
            pipe.hset("test:chat_uid:100", f"9000{i:03d}", str(9000 + i))
        await pipe.execute()
    assert await redis.hlen("test:chat_uid:100") >= 62
    await svc.get_stats(100)
    remaining_uids = cast(dict[str, str], await redis.hgetall("test:chat_uid:100"))
    assert remaining_uids == {w_die.uid: str(w_die.id), w_live.uid: str(w_live.id)}

    # Expired UID is unlocked for re-adding
    assert await svc.find_by_uid(100, "100004") is None
    re_added = await svc.add_watch(100, 1, "100004 | Re-added | 50k")
    assert re_added.uid == "100004"
    await redis.aclose()


@pytest.mark.asyncio
async def test_cmd_start_attaches_persistent_reply_keyboard_and_dashboard() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.filters import CommandObject
    from aiogram.types import ReplyKeyboardMarkup

    from src.modules.watch.handlers import create_watch_router

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisWatchRepository(redis, prefix="test")
    svc = WatchService(repo=repo, checker=_StubChecker({}))
    router = create_watch_router(svc)

    start_handler = next(
        h.callback for h in router.message.handlers if h.callback.__name__ == "cmd_start"
    )

    sent_messages: list[MagicMock] = []

    class _UnhashableSendMethod:
        __hash__ = None  # type: ignore[assignment]

        def __init__(self, text: str, kwargs: dict[str, object]) -> None:
            self._text = text
            self._kwargs = kwargs

        def __await__(self) -> object:
            async def _send() -> MagicMock:
                sent = MagicMock()
                sent.text = self._text
                sent.kwargs = self._kwargs
                sent.delete = AsyncMock()
                sent_messages.append(sent)
                return sent

            return _send().__await__()

    def _fake_answer(text: str, **kwargs: object) -> _UnhashableSendMethod:
        return _UnhashableSendMethod(text, kwargs)

    message = MagicMock()
    message.chat.id = 100
    message.answer = MagicMock(side_effect=_fake_answer)
    state = MagicMock()
    state.clear = AsyncMock()

    await start_handler(
        message=message,
        command=CommandObject(prefix="/", command="start", args=None),
        state=state,
    )

    assert len(sent_messages) == 1
    assert "Đang chờ: <b>0</b>" in sent_messages[0].text
    reply_kb = sent_messages[0].kwargs.get("reply_markup")
    assert isinstance(reply_kb, ReplyKeyboardMarkup)
    assert reply_kb.is_persistent is True
    assert reply_kb.resize_keyboard is True
    assert sum(len(row) for row in reply_kb.keyboard) == 4
    sent_messages[0].delete.assert_not_awaited()

    await redis.aclose()


@pytest.mark.asyncio
async def test_watcher_isolates_transient_notify_failure_and_recheck_uses_lock() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    lock = RedisLock(redis, prefix="test")
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker({"100020": CheckOutcome.DIE, "100021": CheckOutcome.DIE})
    svc = WatchService(repo=repo, checker=checker, lock=lock)

    w_fail = await svc.add_watch(chat_id=100, user_id=1, raw_args="100020 | Chat lỗi mạng | 10k")
    w_ok = await svc.add_watch(chat_id=200, user_id=2, raw_args="100020 | Chat ổn | 20k")
    w_locked = await svc.add_watch(chat_id=300, user_id=3, raw_args="100021 | Đang bị khóa | 30k")

    class _FlakyNotifier:
        def __init__(self) -> None:
            self.fail_chat_100 = True
            self.notified: list[int] = []

        async def notify_live(self, item: WatchItem) -> None:
            if item.chat_id == 100 and self.fail_chat_100:
                raise RuntimeError("transient telegram timeout")
            self.notified.append(item.chat_id)

    notifier = _FlakyNotifier()
    scanner = WatcherScanService(repo=repo, checker=checker, lock=lock, notifier=notifier)

    checker.mapping["100020"] = CheckOutcome.LIVE
    checker.verify_mapping["100020"] = (CheckOutcome.LIVE, "OK")

    # 1st scan: chat 200 succeeds while chat 100 fails transiently -> chat 100 stays DIE & lock released
    assert await scanner.scan_once() == 1
    assert notifier.notified == [200]
    w_fail_after = await repo.get(w_fail.id)
    w_ok_after = await repo.get(w_ok.id)
    assert w_fail_after is not None
    assert w_fail_after.status == WatchStatus.DIE
    assert w_ok_after is not None
    assert w_ok_after.status == WatchStatus.LIVE

    # 2nd scan: transient issue resolved -> chat 100 is notified without losing the alert
    notifier.fail_chat_100 = False
    assert await scanner.scan_once() == 1
    assert notifier.notified == [200, 100]

    # Scanner respects shared RedisLock so it never duplicates a LIVE alert
    checker.mapping["100021"] = CheckOutcome.LIVE
    checker.verify_mapping["100021"] = (CheckOutcome.LIVE, "OK")
    assert await lock.acquire_lock(f"notify:{w_locked.id}", ttl_seconds=60) is True
    assert await scanner.scan_once() == 0
    await redis.aclose()


@pytest.mark.asyncio
async def test_list_lookup_filter_ignores_plain_chat_and_multiline() -> None:
    from unittest.mock import MagicMock

    from src.modules.watch.handlers import create_watch_router

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    svc = WatchService(repo=RedisWatchRepository(redis, prefix="test"), checker=_StubChecker({}))
    router = create_watch_router(svc)
    handler = next(
        h for h in router.message.handlers if h.callback.__name__ == "handle_list_lookup"
    )
    assert handler.filters is not None
    text_filters = handler.filters[1:]

    async def _matches(text: str) -> bool:
        msg = MagicMock()
        msg.text = text
        for flt in text_filters:
            if not await flt.call(msg):
                return False
        return True

    assert await _matches("100012345678901") is True
    assert await _matches("https://fb.com/profile.php?id=100012345678901") is True
    assert await _matches("xin chào mọi người") is False
    assert await _matches("10001\n10002") is False
    await redis.aclose()


@pytest.mark.asyncio
async def test_telegram_watch_notifier_photo_file_id_fallback_retry_and_forbidden() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from aiogram import Bot
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
    from aiogram.methods import SendMessage, SendPhoto

    from src.modules.watch.handlers import TelegramWatchNotifier

    # 1. Downloads avatar bytes via checker, sends BufferedInputFile, and caches returned file_id
    bot = AsyncMock(spec=Bot)
    bot.send_photo.return_value = MagicMock(photo=[MagicMock(file_id="cached_fid_1")])
    notifier = TelegramWatchNotifier(bot, checker=_StubChecker({}))
    item = WatchItem(
        id=1,
        chat_id=10,
        owner_user_id=20,
        uid="4",
        fb_name="Mark Zuckerberg",
        avatar_url="https://scontent.xx.fbcdn.net/zuck.jpg",
        note="VIP",
        price="50k",
        status=WatchStatus.LIVE,
    )
    await notifier.notify_live(item)
    bot.send_photo.assert_awaited_once()
    assert item.avatar_file_id == "cached_fid_1"

    # 2. Cached file_id is reused directly; if Telegram rejects it, falls back cleanly to text
    bot2 = AsyncMock(spec=Bot)
    bad_req = TelegramBadRequest(method=SendPhoto(chat_id=10, photo="x"), message="wrong file id")
    bot2.send_photo.side_effect = bad_req
    notifier2 = TelegramWatchNotifier(bot2, checker=_StubChecker({}))
    await notifier2.notify_live(item)
    bot2.send_photo.assert_awaited_once()
    bot2.send_message.assert_awaited_once()

    # 3. Retries once on TelegramRetryAfter
    bot3 = AsyncMock(spec=Bot)
    bot3.send_message.side_effect = [
        TelegramRetryAfter(
            method=SendMessage(chat_id=10, text="hi"),
            message="Too Many Requests",
            retry_after=0,
        ),
        None,
    ]
    item_no_avt = item.model_copy(update={"avatar_url": "", "avatar_file_id": ""})
    await TelegramWatchNotifier(bot3).notify_live(item_no_avt)
    assert bot3.send_message.await_count == 2

    # 4. Swallows TelegramForbiddenError cleanly without raising
    bot4 = AsyncMock(spec=Bot)
    bot4.send_message.side_effect = TelegramForbiddenError(
        method=SendMessage(chat_id=10, text="hi"), message="bot was blocked"
    )
    await TelegramWatchNotifier(bot4).notify_live(item_no_avt)


@pytest.mark.asyncio
async def test_watch_router_commands_wizard_and_callbacks_end_to_end() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from aiogram import Bot
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from src.modules.watch.handlers import create_watch_router
    from src.modules.watch.ui import (
        ListPageCallback,
        QuickWatchCallback,
        WatchActionCallback,
        WizardCallback,
    )

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    lock = RedisLock(redis, prefix="test")
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker({"100050": CheckOutcome.DIE, "100051": CheckOutcome.LIVE})
    svc = WatchService(repo=repo, checker=checker, lock=lock)
    router = create_watch_router(svc)

    msg_handlers = {h.callback.__name__: h.callback for h in router.message.handlers}
    cb_handlers = {h.callback.__name__: h.callback for h in router.callback_query.handlers}

    class _MemFSM:
        def __init__(self) -> None:
            self.state: object = None
            self.data: dict[str, object] = {}

        async def clear(self) -> None:
            self.state = None
            self.data.clear()

        async def set_state(self, s: object) -> None:
            self.state = s

        async def get_state(self) -> object:
            return self.state

        async def get_data(self) -> dict[str, object]:
            return dict(self.data)

        async def update_data(self, **kwargs: object) -> None:
            self.data.update(kwargs)

    bot = AsyncMock(spec=Bot)
    bot.me = AsyncMock(return_value=MagicMock(username="checklive_bot"))
    bot.edit_message_text = AsyncMock()

    def _msg(text: str = "", chat_id: int = 100, photo: object = None) -> MagicMock:
        m = MagicMock(spec=Message)
        m.chat = MagicMock(id=chat_id)
        m.from_user = MagicMock(id=1)
        m.text = text
        m.photo = photo
        m.message_id = 50
        m.answer = AsyncMock(return_value=MagicMock(message_id=51, delete=AsyncMock()))
        m.answer_photo = AsyncMock(
            return_value=MagicMock(message_id=52, photo=[MagicMock(file_id="fid_99")])
        )
        m.edit_text = AsyncMock()
        m.edit_caption = AsyncMock()
        m.delete = AsyncMock()
        return m

    state = _MemFSM()

    # 1. /help, /cancel, /stats, /list, reply shortcuts & dashboard callbacks
    m_help = _msg()
    await msg_handlers["cmd_help"](m_help, state)
    assert "Hướng dẫn sử dụng" in m_help.answer.call_args.args[0]

    m_cancel = _msg()
    await msg_handlers["cmd_cancel"](m_cancel, state)
    assert "Đã hủy" in m_cancel.answer.call_args.args[0]

    m_stats = _msg()
    await msg_handlers["cmd_stats"](m_stats, state)
    assert "Thống kê" in m_stats.answer.call_args.args[0]

    for btn in ("➕ Lên kèo", "📋 Danh sách", "🔎 Check nhanh", "📊 Thống kê"):
        m_sc = _msg(btn)
        await msg_handlers["handle_reply_shortcuts"](m_sc, state, bot)
        m_sc.answer.assert_awaited()

    # 2. /add valid, duplicate, invalid + /start deep link w_<id> (found & not found)
    m_add = _msg()
    await msg_handlers["cmd_add"](
        m_add, CommandObject(prefix="/", command="add", args="100051 | VIP | 100k | 282"), state
    )
    m_add.answer_photo.assert_awaited_once()

    m_dup = _msg()
    await msg_handlers["cmd_add"](
        m_dup, CommandObject(prefix="/", command="add", args="100051 | Trùng"), state
    )
    assert "đã tồn tại" in m_dup.answer.call_args.args[0]

    m_bad = _msg()
    await msg_handlers["cmd_add"](
        m_bad, CommandObject(prefix="/", command="add", args="not_a_uid"), state
    )
    assert "❌" in m_bad.answer.call_args.args[0]

    m_deep_ok = _msg()
    await msg_handlers["cmd_start"](
        m_deep_ok, CommandObject(prefix="/", command="start", args="w_1"), state
    )
    m_deep_ok.answer_photo.assert_awaited_once()

    m_deep_404 = _msg()
    await msg_handlers["cmd_start"](
        m_deep_404, CommandObject(prefix="/", command="start", args="w_999"), state
    )
    assert "Không tìm thấy" in m_deep_404.answer.call_args.args[0]

    # 3. Full 4-step Wizard flow (invalid UID -> duplicate UID -> valid UID -> note -> pick_price -> skip_faq)
    m_w = _msg()
    await msg_handlers["cmd_add"](m_w, CommandObject(prefix="/", command="add", args=""), state)
    await msg_handlers["wizard_step_uid"](_msg("bad_uid"), state, bot)
    await msg_handlers["wizard_step_uid"](_msg("100051"), state, bot)
    await msg_handlers["wizard_step_uid"](_msg("100050"), state, bot)
    await msg_handlers["wizard_step_note"](_msg("Khách mới"), state, bot)

    q_wz = MagicMock(message=_msg(), from_user=MagicMock(id=1), answer=AsyncMock())
    await cb_handlers["cb_wizard"](q_wz, WizardCallback(action="pick_price", value="200k"), state)
    await cb_handlers["cb_wizard"](q_wz, WizardCallback(action="prev", value="2"), state)
    await msg_handlers["wizard_step_note"](_msg("Khách mới v2"), state, bot)
    await msg_handlers["wizard_step_price"](_msg("300k"), state, bot)
    await msg_handlers["wizard_step_faq"](_msg("282"), state, bot)

    created = await svc.find_by_uid(100, "100050")
    assert created is not None
    assert (created.note, created.price, created.faq) == ("Khách mới v2", "300k", "282")

    # 4. List lookup (found & not found), cb_list_page (text & photo), cb_quick_watch, cb_recheck_all
    m_lu_ok = _msg("100050")
    await msg_handlers["handle_list_lookup"](m_lu_ok)
    m_lu_ok.answer.assert_awaited_once()

    m_lu_miss = _msg("999999999")
    await msg_handlers["handle_list_lookup"](m_lu_miss)
    assert "Không tìm thấy" in m_lu_miss.answer.call_args.args[0]

    q_lp_photo = MagicMock(message=_msg(photo=[MagicMock()]), answer=AsyncMock())
    await cb_handlers["cb_list_page"](
        q_lp_photo, ListPageCallback(status="ALL", page=1), state, bot
    )
    q_lp_photo.message.answer.assert_awaited_once()

    q_qw_dup = MagicMock(message=_msg(), answer=AsyncMock())
    await cb_handlers["cb_quick_watch"](q_qw_dup, QuickWatchCallback(uid="100050"), state)
    q_qw_dup.message.answer.assert_awaited_once()

    q_qw_new = MagicMock(message=_msg(), answer=AsyncMock())
    await cb_handlers["cb_quick_watch"](q_qw_new, QuickWatchCallback(uid="100099"), state)
    q_qw_new.answer.assert_awaited_once()

    # 5. cb_watch_action: hide/show toggle spoiler via edit_media on photo; others edit_caption
    for act, expected_spoiler in (("hide", True), ("show", False)):
        m_photo = _msg(photo=[MagicMock(file_id="fid_1")])
        m_photo.edit_media = AsyncMock()
        q_act = MagicMock(message=m_photo, answer=AsyncMock())
        await cb_handlers["cb_watch_action"](
            q_act, WatchActionCallback(action=act, watch_id=created.id), state
        )
        m_photo.edit_media.assert_awaited_once()
        assert m_photo.edit_media.call_args.kwargs["media"].has_spoiler is expected_spoiler

    for act in ("open", "done", "cancel", "continue", "delete", "delete_cancel"):
        q_act = MagicMock(message=_msg(photo=[MagicMock()]), answer=AsyncMock())
        await cb_handlers["cb_watch_action"](
            q_act, WatchActionCallback(action=act, watch_id=created.id), state
        )
        q_act.message.edit_caption.assert_awaited_once()

    q_edit = MagicMock(message=_msg(), answer=AsyncMock())
    await cb_handlers["cb_watch_action"](
        q_edit, WatchActionCallback(action="edit", watch_id=created.id), state
    )
    q_edit_cancel = MagicMock(message=_msg(), from_user=MagicMock(id=1), answer=AsyncMock())
    await cb_handlers["cb_wizard"](q_edit_cancel, WizardCallback(action="cancel"), state)

    q_bad_act = MagicMock(message=_msg(), answer=AsyncMock())
    await cb_handlers["cb_watch_action"](
        q_bad_act, WatchActionCallback(action="unknown", watch_id=created.id), state
    )
    assert q_bad_act.answer.call_args.kwargs.get("show_alert") is True

    q_del = MagicMock(message=_msg(), answer=AsyncMock())
    await cb_handlers["cb_watch_action"](
        q_del, WatchActionCallback(action="delete_confirm", watch_id=created.id), state
    )
    assert await svc.find_by_uid(100, "100050") is None

    await redis.aclose()


@pytest.mark.asyncio
async def test_watch_service_repo_and_handler_edge_cases() -> None:
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendPhoto
    from aiogram.types import Message

    from src.modules.watch.handlers import _answer_watch_card, create_watch_router  # type: ignore[reportPrivateUsage]
    from src.modules.watch.ui import WizardCallback

    # 1. WatchTtlPolicy validation
    with pytest.raises(ValueError, match="die_days must be >= 1"):
        WatchTtlPolicy(die_days=0)
    with pytest.raises(ValueError, match="seq_days must be >= 0"):
        WatchTtlPolicy(seq_days=-1)

    # 2. Repository edge cases: seq_ttl > 0, stale chat_uid cleanup, list_die_items pruning,
    #    empty compact_chat_uid, set_chat_hidden(False), set_chat_hidden_and_get missing
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisWatchRepository(redis, prefix="edge", ttl_policy=WatchTtlPolicy(seq_days=30))
    assert await repo.compact_chat_uid(999) == 0

    item = await repo.create(
        chat_id=10,
        owner_user_id=1,
        uid="100001",
        note="n",
        price="0",
        faq="",
        status=WatchStatus.DIE,
    )
    assert await redis.ttl("edge:seq:watch_id") > 0

    # Inject stale watch IDs into watches:die (one deleted, one LIVE)
    live_item = await repo.create(
        chat_id=10,
        owner_user_id=1,
        uid="100002",
        note="n2",
        price="0",
        faq="",
        status=WatchStatus.LIVE,
    )
    await redis.sadd("edge:watches:die", "999999", str(live_item.id))
    die_items = await repo.list_die_items()
    assert [i.id for i in die_items] == [item.id]
    assert await redis.smembers("edge:watches:die") == {str(item.id)}

    # Expire watch:{id} -> find_by_uid and find_by_uid_with_hidden prune stale chat_uid hash
    await redis.delete(f"edge:watch:{item.id}")
    assert await repo.find_by_uid(10, "100001") is None
    await redis.hset("edge:chat_uid:10", "100001", str(item.id))
    assert await repo.find_by_uid_with_hidden(10, "100001") is None

    await repo.set_chat_hidden(10, True)
    assert await repo.get_chat_hidden(10) is True
    await repo.set_chat_hidden(10, False)
    assert await repo.get_chat_hidden(10) is False
    assert await repo.set_chat_hidden_and_get(10, 999999, True) is None

    # 3. WatchService: add_watch cancels profile_task on check() error; recheck_chat target_uid & error isolation
    cancelled_uids: list[str] = []

    class _FailingChecker(_StubChecker):
        async def check(self, uid: str) -> UidCheckResult:
            if uid == "100077":
                raise RuntimeError("graph boom")
            return await super().check(uid)

        async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
            if uid == "100077":
                try:
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    cancelled_uids.append(uid)
                    raise
            return await super().fetch_profile_info(uid)

    fail_checker = _FailingChecker(
        {"100080": CheckOutcome.DIE, "100081": CheckOutcome.DIE},
        verify_mapping={"100080": (CheckOutcome.DIE, None)},
    )
    svc = WatchService(repo=repo, checker=fail_checker, max_active_per_chat=0)

    with pytest.raises(RuntimeError, match="graph boom"):
        await svc.add_watch(20, 1, "100077")
    assert cancelled_uids == ["100077"]

    w80 = await svc.add_watch(20, 1, "100080")
    await svc.add_watch(20, 1, "100081")

    with pytest.raises(ValueError, match="Không tìm thấy kèo"):
        await svc.set_hidden(999, w80.id, True)

    # WatcherScanService isolates unexpected checker exception
    scanner = WatcherScanService(
        repo=repo,
        checker=fail_checker,
        lock=RedisLock(redis, prefix="edge"),
        notifier=_StubNotifier(),
    )
    await repo.create(
        chat_id=20,
        owner_user_id=1,
        uid="100077",
        note="err",
        price="0",
        faq="",
        status=WatchStatus.DIE,
    )
    assert await scanner.scan_once() == 0

    # 4. Handlers: _answer_watch_card BufferedInputFile upload + save_avatar_file_id + TelegramBadRequest fallback
    live_card_item = await repo.create(
        chat_id=30,
        owner_user_id=1,
        uid="4",
        fb_name="Mark Zuckerberg",
        avatar_url="https://scontent.xx.fbcdn.net/zuck.jpg",
        note="VIP",
        price="100k",
        faq="",
        status=WatchStatus.LIVE,
    )
    m_card = MagicMock(spec=Message)
    m_card.answer_photo = AsyncMock(return_value=MagicMock(photo=[MagicMock(file_id="saved_fid")]))
    m_card.answer = AsyncMock()
    await _answer_watch_card(m_card, live_card_item, watch_service=svc)
    m_card.answer_photo.assert_awaited_once()
    assert live_card_item.avatar_file_id == "saved_fid"

    m_card_fail = MagicMock(spec=Message)
    m_card_fail.answer_photo = AsyncMock(
        side_effect=TelegramBadRequest(method=SendPhoto(chat_id=30, photo="x"), message="bad")
    )
    m_card_fail.answer = AsyncMock()
    await _answer_watch_card(m_card_fail, live_card_item, watch_service=svc)
    m_card_fail.answer.assert_awaited_once()

    # Refresh expired/missing avatar_url via fetch_profile_info when opening LIVE card
    expired_item = await repo.create(
        chat_id=30,
        owner_user_id=1,
        uid="4",
        fb_name="",
        avatar_url="https://scontent.xx.fbcdn.net/expired.jpg",
        note="Expired CDN",
        price="50k",
        faq="",
        status=WatchStatus.LIVE,
    )
    orig_fetch = fail_checker.fetch_avatar_bytes
    fail_checker.mapping["4"] = CheckOutcome.LIVE

    async def _expired_then_fresh(url: str) -> bytes | None:
        if "expired" in url:
            return None
        return await orig_fetch(url)

    fail_checker.fetch_avatar_bytes = _expired_then_fresh
    m_refresh = MagicMock(spec=Message)
    m_refresh.answer_photo = AsyncMock(
        return_value=MagicMock(photo=[MagicMock(file_id="fresh_fid")])
    )
    m_refresh.answer = AsyncMock()
    await _answer_watch_card(m_refresh, expired_item, watch_service=svc)
    m_refresh.answer_photo.assert_awaited_once()
    assert expired_item.fb_name == "Trang Linh"
    assert expired_item.avatar_url == "https://scontent.xx.fbcdn.net/avatar.jpg"
    assert expired_item.avatar_file_id == "fresh_fid"

    # cb_wizard edge cases: expired draft, recover draft from text + pick_price + skip_faq, empty recheck_all
    router = create_watch_router(svc)
    cb_handlers = {h.callback.__name__: h.callback for h in router.callback_query.handlers}

    state_data: dict[str, object] = {}

    def _update_state(**kw: object) -> None:
        state_data.update(kw)

    state = MagicMock()
    state.get_data = AsyncMock(side_effect=lambda: dict(state_data))
    state.update_data = AsyncMock(side_effect=_update_state)
    state.set_state = AsyncMock()
    state.clear = AsyncMock(side_effect=state_data.clear)

    q_expired = MagicMock(
        message=MagicMock(
            spec=Message, chat=MagicMock(id=30), text="no draft", photo=None, message_id=1
        ),
        from_user=MagicMock(id=1),
        answer=AsyncMock(),
    )
    await cb_handlers["cb_wizard"](q_expired, WizardCallback(action="skip_faq"), state)
    assert q_expired.answer.call_args.kwargs.get("show_alert") is True

    # Non-Message callback returns cleanly
    q_no_msg = MagicMock(message=None, answer=AsyncMock())
    await cb_handlers["cb_wizard"](q_no_msg, WizardCallback(action="cancel"), state)
    q_no_msg.answer.assert_awaited_once()

    # Concurrent add_watch with RedisLock prevents duplicate creation
    add_lock = RedisLock(redis, prefix="edge")
    locked_svc = WatchService(repo=repo, checker=fail_checker, lock=add_lock)
    assert await add_lock.acquire_lock("add:55:100099", ttl_seconds=15) is True
    with pytest.raises(ValueError, match="đang được xử lý"):
        await locked_svc.add_watch(55, 1, "100099")
    await add_lock.release_lock("add:55:100099")

    await redis.aclose()


@pytest.mark.asyncio
async def test_watch_hardening_scan_race_all_ttl_length_caps_and_group_auth() -> None:
    from src.modules.watch.domain import MAX_FAQ_LEN, MAX_NOTE_LEN, MAX_PRICE_LEN

    # 1. Field length validation in parse_add_line & WatchService.update_watch
    with pytest.raises(ValueError, match=str(MAX_NOTE_LEN)):
        parse_add_line(f"10001 | {'n' * (MAX_NOTE_LEN + 1)} | 50k | 282")
    with pytest.raises(ValueError, match=str(MAX_PRICE_LEN)):
        parse_add_line(f"10001 | ok | {'p' * (MAX_PRICE_LEN + 1)} | 282")
    with pytest.raises(ValueError, match=str(MAX_FAQ_LEN)):
        parse_add_line(f"10001 | ok | 50k | {'f' * (MAX_FAQ_LEN + 1)}")

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    policy = WatchTtlPolicy(die_days=90, live_days=60, done_days=30, cancelled_days=14)
    repo = RedisWatchRepository(redis, prefix="hardening", ttl_policy=policy)

    # 2. ALL list keeps a DIE item that turned LIVE at day 85 visible at day 95
    created = await repo.create(
        chat_id=10,
        owner_user_id=1,
        uid="100088",
        note="Old DIE",
        price="50k",
        faq="",
        status=WatchStatus.DIE,
    )
    day_85 = created.created_at + timedelta(days=85)
    created.status = WatchStatus.LIVE
    created.live_detected_at = day_85
    await repo.update(created, previous_status=WatchStatus.DIE)

    day_95 = created.created_at + timedelta(days=95)
    all_items, total, _ = await repo.list_chat_items(10, status_filter=None, now=day_95)
    assert total == 1
    assert [it.id for it in all_items] == [created.id]

    # 3. scan_once() skips deleted/cancelled watches instead of resurrecting them
    class _RaceChecker:
        async def check(self, uid: str) -> UidCheckResult:
            return UidCheckResult(uid=uid, outcome=CheckOutcome.LIVE)

        async def verify_live(self, uid: str) -> UidCheckResult:
            return UidCheckResult(uid=uid, outcome=CheckOutcome.LIVE)

        async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
            # Simulate user deleting the watch while HTTP check is in-flight
            await repo.delete(race_item)
            return "Race User", None

        async def fetch_avatar_bytes(self, url: str) -> bytes | None:
            return None

        async def resolve_uid_from_url(self, url: str) -> str | None:
            return None

        async def close(self) -> None:
            return None

    race_item = await repo.create(
        chat_id=20,
        owner_user_id=1,
        uid="100077",
        note="Race",
        price="0",
        faq="",
        status=WatchStatus.DIE,
    )

    class _FakeNotifier:
        def __init__(self) -> None:
            self.notified: list[int] = []

        async def notify_live(self, item: WatchItem) -> None:
            self.notified.append(item.id)

    notifier = _FakeNotifier()
    scanner = WatcherScanService(
        repo=repo,
        checker=_RaceChecker(),
        lock=RedisLock(redis, prefix="hardening"),
        notifier=notifier,
    )
    assert await scanner.scan_once() == 0
    assert notifier.notified == []
    assert await repo.get(race_item.id) is None

    # 4. Group authorization on destructive watch callbacks
    svc = WatchService(repo=repo, checker=_StubChecker({"100055": CheckOutcome.DIE}))
    group_item = await svc.add_watch(-100777, 10, "100055")
    router = create_watch_router(svc)
    cb_handlers = {h.callback.__name__: h.callback for h in router.callback_query.handlers}
    state = MagicMock()
    state.clear = AsyncMock()

    q_stranger = MagicMock(
        message=MagicMock(
            spec=Message,
            chat=MagicMock(id=-100777, type="group"),
            bot=AsyncMock(get_chat_member=AsyncMock(return_value=MagicMock(status="member"))),
            text="card",
            photo=None,
            message_id=1,
        ),
        from_user=MagicMock(id=99),
        answer=AsyncMock(),
    )
    await cb_handlers["cb_watch_action"](
        q_stranger, WatchActionCallback(action="delete", watch_id=group_item.id), state
    )
    assert q_stranger.answer.call_args.kwargs.get("show_alert") is True

    q_owner = MagicMock(
        message=MagicMock(
            spec=Message,
            chat=MagicMock(id=-100777, type="group"),
            bot=AsyncMock(),
            text="card",
            photo=None,
            message_id=1,
            edit_text=AsyncMock(),
        ),
        from_user=MagicMock(id=10),
        answer=AsyncMock(),
    )
    await cb_handlers["cb_watch_action"](
        q_owner, WatchActionCallback(action="cancel", watch_id=group_item.id), state
    )
    cancelled_item = await repo.get(group_item.id)
    assert cancelled_item is not None
    assert cancelled_item.status == WatchStatus.CANCELLED

    await redis.aclose()
