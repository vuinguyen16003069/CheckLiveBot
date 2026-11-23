from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import fakeredis.aioredis
import pytest

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
from src.modules.watch.service import (
    DuplicateWatchError,
    RedisWatchRepository,
    WatcherScanService,
    WatchService,
)
from src.modules.watch.ui import (
    DealDraft,
    build_dashboard_keyboard,
    build_delete_confirm_keyboard,
    build_list_keyboard,
    build_reply_keyboard,
    build_watch_keyboard,
    build_wizard_keyboard,
    format_wizard_step,
    recover_draft_from_text,
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
    assert len(build_dashboard_keyboard().inline_keyboard) == 2

    draft = DealDraft()
    assert "Bước 1/4" in format_wizard_step(1, draft)
    assert len(build_wizard_keyboard(step=1).inline_keyboard[0]) == 1

    kb3 = build_wizard_keyboard(step=3)
    assert any(btn.text == "50k" for btn in kb3.inline_keyboard[0])
    assert any("Quay lại" in btn.text for btn in kb3.inline_keyboard[1])

    kb4 = build_wizard_keyboard(step=4)
    assert any(btn.text == "282" for btn in kb4.inline_keyboard[0])

    sample_item = WatchItem(id=7, chat_id=1, owner_user_id=1, uid="10001", note="N", price="0")
    list_kb = build_list_keyboard("ALL", 2, 3)
    flat_texts = [btn.text for row in list_kb.inline_keyboard for btn in row]
    assert "🔄 Quét lại DIE" in flat_texts
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
        [sample_item],
        status_filter="ALL",
        page=1,
        total_pages=1,
        total_items=1,
        bot_username="my_checklive_bot",
    )
    assert 'href="https://t.me/my_checklive_bot?start=w_7"' in list_html

    del_kb = build_delete_confirm_keyboard(7)
    assert any("Xác nhận xóa #7" in btn.text for btn in del_kb.inline_keyboard[0])

    die_kb = build_watch_keyboard(sample_item)
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

    recovered = recover_draft_from_text(
        "📌 LÊN KÈO NHANH\n\n🆔 UID: 100057257853932\n📝 Ghi chú: Zalo Vui\n💰 Giá: 50k\n"
    )
    assert (recovered.uid, recovered.note, recovered.price, recovered.edit_watch_id) == (
        "100057257853932",
        "Zalo Vui",
        "50k",
        None,
    )
    recovered_edit = recover_draft_from_text(
        "✏️ SỬA KÈO #7\n\n🆔 UID: 10001\n📝 Ghi chú: Khách mới\n"
    )
    assert (recovered_edit.edit_watch_id, recovered_edit.uid, recovered_edit.note) == (
        7,
        "10001",
        "Khách mới",
    )
    assert recover_draft_from_text(None) == DealDraft()


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

    # Global per-chat hide preference persists across all watches in chat 100
    w1_hidden = await svc.set_hidden(chat_id=100, watch_id=w1.id, hidden=True)
    assert w1_hidden.is_hidden is True
    ttl_pref = await redis.ttl("test:pref:hidden:100")
    assert 89 * 86400 < ttl_pref <= 90 * 86400
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

    # recheck_chat: 100001 verified LIVE, 100003 false LIVE rejected at layer 2
    checker.mapping["100001"] = CheckOutcome.LIVE
    checker.mapping["100003"] = CheckOutcome.LIVE
    checker.verify_mapping["100001"] = (CheckOutcome.LIVE, "OK")
    checker.verify_mapping["100003"] = (CheckOutcome.DIE, None)

    scanned, turned_live = await svc.recheck_chat(100)
    assert scanned == 2
    assert [it.id for it in turned_live] == [w1.id]

    # target_uid filter & empty recheck
    scanned_one, live_one = await svc.recheck_chat(100, target_uid="100003")
    assert (scanned_one, live_one) == (1, [])
    assert await svc.recheck_chat(999) == (0, [])

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
async def test_unified_90d_ttl_on_all_keys_and_lazy_prune() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisWatchRepository(redis, prefix="test", ttl_days=90)
    checker = _StubChecker(
        {
            "100001": CheckOutcome.DIE,
            "100002": CheckOutcome.LIVE,
            "100003": CheckOutcome.DIE,
        }
    )
    svc = WatchService(repo=repo, checker=checker)

    w_die = await svc.add_watch(100, 1, "100001 | Old DIE | 10k")
    w_live = await svc.add_watch(100, 1, "100002 | Recent LIVE | 20k")
    w_done = await svc.add_watch(100, 1, "100003 | Old Done | 30k")
    w_done = await svc.mark_status(100, w_done.id, WatchStatus.DONE)
    await svc.set_hidden(100, w_die.id, True)

    # Zero watches:expiry key; every single key in Redis MUST have ~90-day TTL
    all_keys = [k async for k in cast(AsyncIterator[str], redis.scan_iter("*"))]
    assert len(all_keys) > 0
    assert "test:watches:expiry" not in all_keys
    for key in all_keys:
        ttl = await redis.ttl(key)
        assert 89 * 86400 < ttl <= 90 * 86400, f"Key {key} missing 90d TTL (ttl={ttl})"

    # Simulate w_die & w_done aged 95 days (Redis TTL expired their watch:* keys)
    old_ts = (datetime.now(UTC) - timedelta(days=95)).timestamp()
    await redis.zadd("test:watches:chat:100", {str(w_die.id): old_ts, str(w_done.id): old_ts})
    await redis.zadd("test:watches:chat:100:status:DIE", {str(w_die.id): old_ts})
    await redis.zadd("test:watches:chat:100:status:DONE", {str(w_done.id): old_ts})
    await redis.delete(f"test:watch:{w_die.id}", f"test:watch:{w_done.id}")

    # Inject a non-DIE stale id into watches:die to verify list_die_items prunes both kinds
    await redis.sadd("test:watches:die", str(w_live.id))  # type: ignore[misc]

    # On-demand pruning via ZREMRANGEBYSCORE & lazy SREM/HDEL (no background cron!)
    stats = await svc.get_stats(100)
    assert (stats.total, stats.die, stats.live, stats.done, stats.cancelled) == (1, 0, 1, 0, 0)

    items, total, _ = await svc.list_watches(100)
    assert total == 1
    assert [it.id for it in items] == [w_live.id]

    assert await repo.list_die_items() == []
    assert cast(int, await redis.scard("test:watches:die")) == 0  # type: ignore[misc]

    # Expired UID is lazily pruned from chat_uid and unlocked for re-adding
    assert await svc.find_by_uid(100, "100001") is None
    re_added = await svc.add_watch(100, 1, "100001 | Re-added | 50k")
    assert re_added.uid == "100001"
    await redis.aclose()
