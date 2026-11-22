from __future__ import annotations

import fakeredis.aioredis
import pytest

from src.core.ports import CheckOutcome, UidCheckResult
from src.core.redis import RedisCache
from src.modules.watch.domain import (
    WatchItem,
    WatchStatus,
    format_watch_card,
    parse_add_line,
)
from src.modules.watch.service import RedisWatchRepository, WatcherScanService, WatchService


class _StubChecker:
    def __init__(self, mapping: dict[str, CheckOutcome]) -> None:
        self.mapping = mapping

    async def check(self, uid: str) -> UidCheckResult:
        return UidCheckResult(uid=uid, outcome=self.mapping.get(uid, CheckOutcome.DIE))

    async def close(self) -> None:
        return None


class _StubNotifier:
    def __init__(self) -> None:
        self.notified: list[WatchItem] = []

    async def notify_live(self, item: WatchItem) -> None:
        self.notified.append(item)


def test_parse_add_line_and_html_escape() -> None:
    parsed = parse_add_line(
        "https://facebook.com/profile.php?id=100012345678901 | <VIP> & Co | 50k | FAQ?"
    )
    assert parsed.uid == "100012345678901"
    assert parsed.note == "<VIP> & Co"
    assert parsed.price == "50k"
    assert parsed.faq == "FAQ?"

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
    assert "100012345678901" in card

    item.is_hidden = True
    hidden_card = format_watch_card(item)
    assert "<tg-spoiler><code>100012345678901</code></tg-spoiler>" in hidden_card
    assert "<tg-spoiler>&lt;VIP&gt; &amp; Co</tg-spoiler>" in hidden_card

    with pytest.raises(ValueError):
        parse_add_line("invalid_line")

    # Flexible styles: "UID note | price" and bare "UID"
    compact = parse_add_line("100012345678901 Khách VIP | 90k")
    assert (compact.uid, compact.note, compact.price) == ("100012345678901", "Khách VIP", "90k")
    bare = parse_add_line("100012345678901")
    assert bare.uid == "100012345678901"


@pytest.mark.asyncio
async def test_watch_upsert_search_and_delete() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = RedisCache(redis, prefix="test")
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker({"100099": CheckOutcome.DIE, "20531316728": CheckOutcome.LIVE})
    svc = WatchService(repo=repo, checker=checker, cache=cache)

    item1, updated1 = await svc.upsert_watch(100, 1, "100099 | Khách cũ | 10k")
    item2, updated2 = await svc.upsert_watch(100, 1, "100099 | Khách mới | 50k")
    assert updated1 is False
    assert updated2 is True
    assert item1.id == item2.id
    assert item2.note == "Khách mới"
    assert item2.price == "50k"

    # Also deduplicates LIVE watches
    live1, live_upd1 = await svc.upsert_watch(100, 1, "20531316728 | FB 1 | 0")
    live2, live_upd2 = await svc.upsert_watch(100, 1, "20531316728 | FB 2 | 10k")
    assert live_upd1 is False
    assert live_upd2 is True
    assert live1.id == live2.id
    assert live2.note == "FB 2"

    found = await svc.search_watches(100, "Khách mới")
    assert len(found) == 1 and found[0].id == item1.id

    await svc.delete_watch(100, item1.id)
    assert await svc.search_watches(100, "100099") == []
    await redis.aclose()


@pytest.mark.asyncio
async def test_deal_form_ui_and_update_details() -> None:
    from src.modules.watch.ui import DealDraft, build_deal_form_keyboard, format_deal_form

    draft = DealDraft(uid="100012345678901", note="<Khách A>", price="150k")
    form_text = format_deal_form(draft)
    assert "LÊN KÈO MỚI" in form_text
    assert "100012345678901" in form_text
    assert "&lt;Khách A&gt;" in form_text
    kb_create = build_deal_form_keyboard(draft)
    assert len(kb_create.inline_keyboard) == 3

    edit_draft = DealDraft(watch_id=5, uid="100012345678901", note="A", price="100k")
    assert "SỬA KÈO #5" in format_deal_form(edit_draft)
    kb_edit = build_deal_form_keyboard(edit_draft)
    assert any("Lưu thay đổi" in btn.text for row in kb_edit.inline_keyboard for btn in row)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = RedisCache(redis, prefix="test")
    repo = RedisWatchRepository(redis, prefix="test")
    svc = WatchService(repo=repo, checker=_StubChecker({"100088": CheckOutcome.DIE}), cache=cache)

    item = await svc.add_watch(100, 1, "100088 | Cũ | 10k")
    updated = await svc.update_watch_details(100, item.id, note="Mới", price="99k", faq="FAQ 1")
    assert (updated.note, updated.price, updated.faq) == ("Mới", "99k", "FAQ 1")

    with pytest.raises(ValueError):
        await svc.update_watch_details(999, item.id, note="Hack", price="0", faq="")
    await redis.aclose()


def test_wizard_steps_and_cancel_keyboard() -> None:
    from src.modules.watch.ui import DealDraft, build_wizard_keyboard, format_wizard_step

    draft = DealDraft()
    assert "Bước 1/4" in format_wizard_step(1, draft)
    kb1 = build_wizard_keyboard(allow_skip=False)
    assert len(kb1.inline_keyboard[0]) == 1
    assert "Hủy" in kb1.inline_keyboard[0][0].text

    draft.uid = "100012345678901"
    draft.note = "Khách A"
    draft.price = "150k"
    step4 = format_wizard_step(4, draft)
    assert "Bước 4/4" in step4
    assert "100012345678901" in step4
    assert "Khách A" in step4
    kb4 = build_wizard_keyboard(allow_skip=True)
    assert len(kb4.inline_keyboard[0]) == 2
    assert "Bỏ qua" in kb4.inline_keyboard[0][0].text
    assert "Hủy" in kb4.inline_keyboard[0][1].text


@pytest.mark.asyncio
async def test_watch_service_lifecycle_pagination_and_isolation() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = RedisCache(redis, prefix="test")
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker({"100001": CheckOutcome.DIE, "100002": CheckOutcome.LIVE})
    svc = WatchService(repo=repo, checker=checker, cache=cache, max_active_per_chat=10)

    w1 = await svc.add_watch(chat_id=100, user_id=1, raw_args="100001 | Khách 1 | 50k")
    w2 = await svc.add_watch(chat_id=100, user_id=1, raw_args="100002 | Khách 2 | 80k")
    assert w1.status == WatchStatus.DIE
    assert w2.status == WatchStatus.LIVE

    # Isolation: chat 200 cannot mutate chat 100's watch
    with pytest.raises(ValueError):
        await svc.mark_status(chat_id=200, watch_id=w1.id, new_status=WatchStatus.DONE)

    # Toggle visibility & status transitions
    w1_hidden = await svc.set_hidden(chat_id=100, watch_id=w1.id, hidden=True)
    assert w1_hidden.is_hidden is True

    w1_done = await svc.mark_status(chat_id=100, watch_id=w1.id, new_status=WatchStatus.DONE)
    assert w1_done.status == WatchStatus.DONE

    stats = await svc.get_stats(chat_id=100)
    assert stats.total == 2
    assert stats.done == 1
    assert stats.live == 1
    assert stats.die == 0

    # Resume watching puts it back to DIE
    w1_resumed = await svc.mark_status(chat_id=100, watch_id=w1.id, new_status=WatchStatus.DIE)
    assert w1_resumed.status == WatchStatus.DIE

    items, total, pages = await svc.list_watches(chat_id=100, status_filter=WatchStatus.DIE, page=1)
    assert total == 1
    assert pages == 1
    assert items[0].id == w1.id

    events = await svc.get_history(chat_id=100, limit=10)
    assert len(events) >= 3

    await redis.aclose()


@pytest.mark.asyncio
async def test_watcher_scan_skips_error_and_notifies_once_on_live() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = RedisCache(redis, prefix="test")
    repo = RedisWatchRepository(redis, prefix="test")
    checker = _StubChecker({"100010": CheckOutcome.DIE, "100011": CheckOutcome.DIE})
    svc = WatchService(repo=repo, checker=checker, cache=cache)
    notifier = _StubNotifier()
    scanner = WatcherScanService(repo=repo, checker=checker, cache=cache, notifier=notifier)

    await svc.add_watch(chat_id=100, user_id=1, raw_args="100010 | A | 10k")
    await svc.add_watch(chat_id=100, user_id=1, raw_args="100011 | B | 20k")

    # 100010 returns ERROR (should NOT notify or flip), 100011 turns LIVE
    checker.mapping["100010"] = CheckOutcome.ERROR
    checker.mapping["100011"] = CheckOutcome.LIVE

    count = await scanner.scan_once()
    assert count == 1
    assert len(notifier.notified) == 1
    assert notifier.notified[0].uid == "100011"

    # Scanning again must not re-notify
    count_again = await scanner.scan_once()
    assert count_again == 0

    await redis.aclose()
