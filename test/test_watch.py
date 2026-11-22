from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from src.core.ports import CheckOutcome, UidCheckResult
from src.core.redis import RedisLock
from src.modules.watch.domain import (
    WatchItem,
    WatchStats,
    WatchStatus,
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
    build_delete_confirm_keyboard,
    build_list_keyboard,
    build_reply_keyboard,
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


def test_parse_add_line_and_html_escape_and_spoiler() -> None:
    parsed = parse_add_line(
        "https://facebook.com/profile.php?id=100012345678901 | <VIP> & Co | 50k | FAQ?"
    )
    assert (parsed.uid, parsed.note, parsed.price, parsed.faq) == (
        "100012345678901",
        "<VIP> & Co",
        "50k",
        "FAQ?",
    )

    item = WatchItem(
        id=1,
        chat_id=10,
        owner_user_id=20,
        uid=parsed.uid,
        note=parsed.note,
        price=parsed.price,
        faq=parsed.faq,
    )
    card = format_watch_card(item)
    assert "&lt;VIP&gt; &amp; Co" in card

    item.is_hidden = True
    hidden_card = format_watch_card(item)
    assert "<tg-spoiler>100012345678901</tg-spoiler>" in hidden_card
    assert "<b>Thời gian:</b> <tg-spoiler>" in hidden_card

    with pytest.raises(ValueError):
        parse_add_line("invalid_line")

    bare = parse_add_line("100012345678901")
    assert bare.uid == "100012345678901"


@pytest.mark.asyncio
async def test_watch_duplicate_rejection_and_delete() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker({"100099": CheckOutcome.DIE, "20531316728": CheckOutcome.LIVE})
    svc = WatchService(repo=repo, checker=checker)

    item1 = await svc.add_watch(100, 1, "100099 | Khách cũ | 10k")
    with pytest.raises(DuplicateWatchError) as exc_info:
        await svc.add_watch(100, 1, "100099 | Khách mới | 50k")
    assert exc_info.value.existing.id == item1.id

    live1 = await svc.add_watch(100, 1, "20531316728 | FB 1 | 0")
    with pytest.raises(DuplicateWatchError) as live_exc:
        await svc.add_watch(100, 1, "20531316728 | FB 2 | 10k")
    assert live_exc.value.existing.id == live1.id

    await svc.delete_watch(100, item1.id)
    re_added = await svc.add_watch(100, 1, "100099 | Lên lại | 50k")
    assert re_added.uid == "100099"
    await redis.aclose()


def test_ui_keyboards_and_wizard_recovery() -> None:
    stats = WatchStats(total=10, die=4, live=2, done=4, cancelled=0)
    assert "Đang chờ: <b>4</b>" in format_welcome_dashboard(stats)
    assert "Tổng số kèo: <b>10</b>" in format_watch_stats(stats)

    rk = build_reply_keyboard()
    assert len(rk.keyboard) == 2 and len(rk.keyboard[0]) == 2

    draft = DealDraft()
    assert "Bước 1/4" in format_wizard_step(1, draft)
    assert len(build_wizard_keyboard(step=1).inline_keyboard[0]) == 1

    kb3 = build_wizard_keyboard(step=3)
    assert any(btn.text == "50k" for btn in kb3.inline_keyboard[0])
    assert any("Quay lại" in btn.text for btn in kb3.inline_keyboard[1])

    kb4 = build_wizard_keyboard(step=4)
    assert any(btn.text == "282" for btn in kb4.inline_keyboard[0])

    sample_item = WatchItem(id=7, chat_id=1, owner_user_id=1, uid="10001", note="N", price="0")
    list_kb = build_list_keyboard("ALL", 1, 1)
    flat_texts = [btn.text for row in list_kb.inline_keyboard for btn in row]
    assert "🔄 Quét lại DIE" in flat_texts

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

    recovered = recover_draft_from_text(
        "📌 LÊN KÈO NHANH\n\n🆔 UID: 100057257853932\n📝 Ghi chú: Zalo Vui\n💰 Giá: 50k\n"
    )
    assert (recovered.uid, recovered.note, recovered.price) == (
        "100057257853932",
        "Zalo Vui",
        "50k",
    )


@pytest.mark.asyncio
async def test_watch_service_lifecycle_pagination_and_isolation() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker({"100001": CheckOutcome.DIE, "100002": CheckOutcome.LIVE})
    svc = WatchService(repo=repo, checker=checker, max_active_per_chat=10)

    w1 = await svc.add_watch(chat_id=100, user_id=1, raw_args="100001 | Khách 1 | 50k")
    w2 = await svc.add_watch(chat_id=100, user_id=1, raw_args="100002 | Khách 2 | 80k")
    assert w1.status == WatchStatus.DIE
    assert w2.status == WatchStatus.LIVE

    with pytest.raises(ValueError):
        await svc.mark_status(chat_id=200, watch_id=w1.id, new_status=WatchStatus.DONE)

    w1_hidden = await svc.set_hidden(chat_id=100, watch_id=w1.id, hidden=True)
    assert w1_hidden.is_hidden is True

    w1_done = await svc.mark_status(chat_id=100, watch_id=w1.id, new_status=WatchStatus.DONE)
    assert w1_done.status == WatchStatus.DONE

    stats = await svc.get_stats(chat_id=100)
    assert (stats.total, stats.done, stats.live, stats.die) == (2, 1, 1, 0)

    w1_resumed = await svc.mark_status(chat_id=100, watch_id=w1.id, new_status=WatchStatus.DIE)
    assert w1_resumed.status == WatchStatus.DIE

    items, total, pages = await svc.list_watches(chat_id=100, status_filter=WatchStatus.DIE, page=1)
    assert total == 1 and pages == 1 and items[0].id == w1.id
    await redis.aclose()


@pytest.mark.asyncio
async def test_watcher_double_verify_prevents_false_live_and_extracts_fb_name() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    lock = RedisLock(redis, prefix="test")
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker(
        {"100010": CheckOutcome.DIE, "100011": CheckOutcome.DIE, "100012": CheckOutcome.DIE}
    )
    svc = WatchService(repo=repo, checker=checker)
    notifier = _StubNotifier()
    scanner = WatcherScanService(repo=repo, checker=checker, lock=lock, notifier=notifier)

    await svc.add_watch(chat_id=100, user_id=1, raw_args="100010 | Error | 10k")
    await svc.add_watch(chat_id=100, user_id=1, raw_args="100011 | Real Live | 20k")
    await svc.add_watch(chat_id=200, user_id=2, raw_args="100011 | Same UID Chat 2 | 25k")
    await svc.add_watch(chat_id=100, user_id=1, raw_args="100012 | False Live | 30k")

    # 100010 -> ERROR; 100011 -> LIVE + verified LIVE ("Trang Linh"); 100012 -> LIVE layer 1 but DIE layer 2!
    checker.mapping["100010"] = CheckOutcome.ERROR
    checker.mapping["100011"] = CheckOutcome.LIVE
    checker.mapping["100012"] = CheckOutcome.LIVE
    checker.verify_mapping["100011"] = (CheckOutcome.LIVE, "Trang Linh")
    checker.verify_mapping["100012"] = (CheckOutcome.DIE, None)

    checker.check_calls.clear()
    count = await scanner.scan_once()
    assert count == 2
    assert checker.check_calls.count("100011") == 1
    assert len(notifier.notified) == 2
    assert {n.chat_id for n in notifier.notified} == {100, 200}

    # Scanning again must not re-notify 100011
    checker.verify_mapping["100012"] = (CheckOutcome.DIE, None)
    assert await scanner.scan_once() == 0
    await redis.aclose()


@pytest.mark.asyncio
async def test_live_60d_cleanup_archives_to_done_and_protects_others() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisWatchRepository(redis, prefix="test", live_retention_days=60)
    checker = _StubChecker(
        {
            "100001": CheckOutcome.DIE,
            "100002": CheckOutcome.LIVE,
            "100003": CheckOutcome.DIE,
            "100004": CheckOutcome.LIVE,
        }
    )
    svc = WatchService(repo=repo, checker=checker)

    w_die = await svc.add_watch(100, 1, "100001 | Active DIE | 10k")
    w_live_recent = await svc.add_watch(100, 1, "100002 | Recent LIVE | 20k")
    w_done = await svc.add_watch(100, 1, "100003 | Done Deal | 30k")
    w_live_old = await svc.add_watch(100, 1, "100004 | Forgotten LIVE | 50k")

    # Only LIVE gets 60-day TTL; DIE and DONE have no TTL
    assert await redis.ttl(f"test:watch:{w_die.id}") == -1
    ttl_live = await redis.ttl(f"test:watch:{w_live_recent.id}")
    assert 59 * 86400 < ttl_live <= 60 * 86400

    w_done = await svc.mark_status(100, w_done.id, WatchStatus.DONE)
    assert await redis.ttl(f"test:watch:{w_done.id}") == -1

    # Resuming LIVE -> DIE removes TTL
    w_live_recent = await svc.mark_status(100, w_live_recent.id, WatchStatus.DIE)
    assert await redis.ttl(f"test:watch:{w_live_recent.id}") == -1
    w_live_recent.status = WatchStatus.LIVE
    await repo.update(w_live_recent, previous_status=WatchStatus.DIE)

    # Backdate forgotten LIVE to 65 days ago & simulate Redis TTL eviction
    now = datetime.now(UTC)
    w_live_old.live_detected_at = now - timedelta(days=65)
    await repo.update(w_live_old)
    await redis.delete(f"test:watch:{w_live_old.id}")

    cleaned = await repo.cleanup_expired(now=now)
    assert cleaned == 1

    # DIE, DONE, and recent LIVE remain untouched; forgotten LIVE (>60d) pruned
    assert await repo.get(w_die.id) is not None
    assert await repo.get(w_done.id) is not None
    assert await repo.get(w_live_recent.id) is not None
    assert await repo.get(w_live_old.id) is None

    # Forgotten LIVE archived into DONE stats (done=2, live=1, die=1, total=4)
    stats = await svc.get_stats(100)
    assert (stats.total, stats.die, stats.live, stats.done, stats.cancelled) == (4, 1, 1, 2, 0)

    # Pruned UID is unlocked for re-adding
    re_added = await svc.add_watch(100, 1, "100004 | New Deal | 50k")
    assert re_added.uid == "100004"
    await redis.aclose()
