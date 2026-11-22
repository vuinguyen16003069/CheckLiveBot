from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Protocol

import structlog
from redis.asyncio import Redis

from src.core.ports import CachePort, CheckOutcome, FacebookCheckerPort
from src.modules.watch.domain import (
    WatchEvent,
    WatchItem,
    WatchStats,
    WatchStatus,
    parse_add_line,
)

logger = structlog.get_logger(__name__)


class WatchNotifierPort(Protocol):
    async def notify_live(self, item: WatchItem) -> None: ...


class RedisWatchRepository:
    def __init__(self, redis: Redis, prefix: str = "checklivebot") -> None:
        self._redis = redis
        self._prefix = prefix

    def _k(self, suffix: str) -> str:
        return f"{self._prefix}:{suffix}"

    async def create(
        self,
        *,
        chat_id: int,
        owner_user_id: int,
        uid: str,
        note: str,
        price: str,
        faq: str,
        status: WatchStatus,
    ) -> WatchItem:
        watch_id = int(await self._redis.incr(self._k("seq:watch_id")))
        now = datetime.now(UTC)
        item = WatchItem(
            id=watch_id,
            chat_id=chat_id,
            owner_user_id=owner_user_id,
            uid=uid,
            note=note,
            price=price,
            faq=faq,
            status=status,
            created_at=now,
            last_checked_at=now,
            live_detected_at=now if status == WatchStatus.LIVE else None,
        )
        score = now.timestamp()
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._k(f"watch:{watch_id}"), item.model_dump_json())
            pipe.zadd(self._k(f"watches:chat:{chat_id}"), {str(watch_id): score})
            pipe.zadd(
                self._k(f"watches:chat:{chat_id}:status:{status.value}"),
                {str(watch_id): score},
            )
            if status == WatchStatus.DIE:
                pipe.sadd(self._k("watches:die"), str(watch_id))
            await pipe.execute()

        await self.append_event(
            WatchEvent(
                watch_id=watch_id,
                chat_id=chat_id,
                uid=uid,
                event_type="CREATED",
                detail=status.value,
            )
        )
        return item

    async def get(self, watch_id: int) -> WatchItem | None:
        raw = await self._redis.get(self._k(f"watch:{watch_id}"))
        if not raw:
            return None
        return WatchItem.model_validate_json(raw)

    async def update(
        self,
        item: WatchItem,
        *,
        previous_status: WatchStatus | None = None,
    ) -> WatchItem:
        score = item.created_at.timestamp()
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._k(f"watch:{item.id}"), item.model_dump_json())
            if previous_status is not None and previous_status != item.status:
                pipe.zrem(
                    self._k(f"watches:chat:{item.chat_id}:status:{previous_status.value}"),
                    str(item.id),
                )
                pipe.zadd(
                    self._k(f"watches:chat:{item.chat_id}:status:{item.status.value}"),
                    {str(item.id): score},
                )
            if item.status == WatchStatus.DIE:
                pipe.sadd(self._k("watches:die"), str(item.id))
            else:
                pipe.srem(self._k("watches:die"), str(item.id))
            await pipe.execute()
        return item

    async def count_active_die(self, chat_id: int) -> int:
        return int(await self._redis.zcard(self._k(f"watches:chat:{chat_id}:status:DIE")))

    async def list_die_items(self) -> list[WatchItem]:
        ids = await self._redis.smembers(self._k("watches:die"))  # type: ignore[misc]
        if not ids:
            return []
        keys = [self._k(f"watch:{wid}") for wid in ids]
        raws = await self._redis.mget(keys)
        items: list[WatchItem] = []
        for raw in raws:
            if raw:
                item = WatchItem.model_validate_json(raw)
                if item.status == WatchStatus.DIE:
                    items.append(item)
        return items

    async def list_chat_items(
        self,
        chat_id: int,
        *,
        status_filter: WatchStatus | None = None,
        page: int = 1,
        page_size: int = 8,
    ) -> tuple[list[WatchItem], int, int]:
        key = (
            self._k(f"watches:chat:{chat_id}:status:{status_filter.value}")
            if status_filter
            else self._k(f"watches:chat:{chat_id}")
        )
        total = int(await self._redis.zcard(key))
        total_pages = max(1, math.ceil(total / page_size))
        safe_page = min(max(1, page), total_pages)
        start = (safe_page - 1) * page_size
        end = start + page_size - 1
        ids = await self._redis.zrevrange(key, start, end)
        if not ids:
            return [], total, total_pages
        raws = await self._redis.mget([self._k(f"watch:{wid}") for wid in ids])
        items = [WatchItem.model_validate_json(raw) for raw in raws if raw]
        return items, total, total_pages

    async def get_stats(self, chat_id: int) -> WatchStats:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.zcard(self._k(f"watches:chat:{chat_id}"))
            pipe.zcard(self._k(f"watches:chat:{chat_id}:status:DIE"))
            pipe.zcard(self._k(f"watches:chat:{chat_id}:status:LIVE"))
            pipe.zcard(self._k(f"watches:chat:{chat_id}:status:DONE"))
            pipe.zcard(self._k(f"watches:chat:{chat_id}:status:CANCELLED"))
            total, die, live, done, cancelled = await pipe.execute()
        return WatchStats(
            total=int(total),
            die=int(die),
            live=int(live),
            done=int(done),
            cancelled=int(cancelled),
        )

    async def append_event(self, event: WatchEvent) -> None:
        key = self._k(f"events:chat:{event.chat_id}")
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.lpush(key, event.model_dump_json())
            pipe.ltrim(key, 0, 99)
            await pipe.execute()

    async def list_events(self, chat_id: int, limit: int = 10) -> list[WatchEvent]:
        raws = await self._redis.lrange(self._k(f"events:chat:{chat_id}"), 0, max(0, limit - 1))  # type: ignore[misc]
        return [WatchEvent.model_validate_json(raw) for raw in raws]


class WatchService:
    def __init__(
        self,
        *,
        repo: RedisWatchRepository,
        checker: FacebookCheckerPort,
        cache: CachePort,
        cache_ttl_seconds: int = 600,
        max_active_per_chat: int = 100,
        concurrency: int = 20,
    ) -> None:
        self._repo = repo
        self._checker = checker
        self._cache = cache
        self._cache_ttl = cache_ttl_seconds
        self._max_active_per_chat = max_active_per_chat
        self._concurrency = max(1, concurrency)

    async def add_watch(self, chat_id: int, user_id: int, raw_args: str) -> WatchItem:
        parsed = parse_add_line(raw_args)
        if self._max_active_per_chat > 0:
            active_count = await self._repo.count_active_die(chat_id)
            if active_count >= self._max_active_per_chat:
                raise ValueError(
                    f"Chat đã đạt giới hạn {self._max_active_per_chat} kèo DIE đang theo dõi."
                )

        cached = await self._cache.get_cached_outcomes([parsed.uid])
        if parsed.uid in cached:
            outcome = cached[parsed.uid]
        else:
            res = await self._checker.check(parsed.uid)
            outcome = res.outcome
            await self._cache.set_cached_outcome(parsed.uid, outcome, self._cache_ttl)

        initial_status = WatchStatus.LIVE if outcome == CheckOutcome.LIVE else WatchStatus.DIE
        return await self._repo.create(
            chat_id=chat_id,
            owner_user_id=user_id,
            uid=parsed.uid,
            note=parsed.note,
            price=parsed.price,
            faq=parsed.faq,
            status=initial_status,
        )

    async def add_watch_batch(
        self,
        chat_id: int,
        user_id: int,
        raw_text: str,
    ) -> tuple[list[WatchItem], list[str]]:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        added: list[WatchItem] = []
        errors: list[str] = []
        for idx, line in enumerate(lines, start=1):
            try:
                item = await self.add_watch(chat_id, user_id, line)
                added.append(item)
            except ValueError as exc:
                errors.append(f"Dòng {idx}: {exc}")
        return added, errors

    async def list_watches(
        self,
        chat_id: int,
        *,
        status_filter: WatchStatus | None = None,
        page: int = 1,
    ) -> tuple[list[WatchItem], int, int]:
        return await self._repo.list_chat_items(
            chat_id,
            status_filter=status_filter,
            page=page,
            page_size=8,
        )

    async def get_stats(self, chat_id: int) -> WatchStats:
        return await self._repo.get_stats(chat_id)

    async def get_history(self, chat_id: int, limit: int = 10) -> list[WatchEvent]:
        return await self._repo.list_events(chat_id, limit=limit)

    async def recheck_chat(
        self,
        chat_id: int,
        target_uid: str | None = None,
    ) -> tuple[int, list[WatchItem]]:
        items, _, _ = await self._repo.list_chat_items(
            chat_id,
            status_filter=WatchStatus.DIE,
            page=1,
            page_size=500,
        )
        if target_uid:
            items = [i for i in items if i.uid == target_uid]
        if not items:
            return 0, []

        sem = asyncio.Semaphore(self._concurrency)
        turned_live: list[WatchItem] = []

        async def _check_item(item: WatchItem) -> None:
            async with sem:
                res = await self._checker.check(item.uid)
                now = datetime.now(UTC)
                item.last_checked_at = now
                if res.outcome == CheckOutcome.LIVE:
                    item.status = WatchStatus.LIVE
                    item.live_detected_at = now
                    await self._repo.update(item, previous_status=WatchStatus.DIE)
                    await self._cache.set_cached_outcome(
                        item.uid,
                        CheckOutcome.LIVE,
                        self._cache_ttl,
                    )
                    await self._repo.append_event(
                        WatchEvent(
                            watch_id=item.id,
                            chat_id=item.chat_id,
                            uid=item.uid,
                            event_type="LIVE_DETECTED",
                            detail="recheck",
                        )
                    )
                    turned_live.append(item)
                elif res.outcome == CheckOutcome.DIE:
                    await self._repo.update(item)

        await asyncio.gather(*(_check_item(i) for i in items))
        return len(items), turned_live

    async def _require_owned(self, chat_id: int, watch_id: int) -> WatchItem:
        item = await self._repo.get(watch_id)
        if item is None or item.chat_id != chat_id:
            raise ValueError("Không tìm thấy kèo trong chat này.")
        return item

    async def set_hidden(self, chat_id: int, watch_id: int, hidden: bool) -> WatchItem:
        item = await self._require_owned(chat_id, watch_id)
        item.is_hidden = hidden
        return await self._repo.update(item)

    async def mark_status(
        self,
        chat_id: int,
        watch_id: int,
        new_status: WatchStatus,
    ) -> WatchItem:
        item = await self._require_owned(chat_id, watch_id)
        prev = item.status
        item.status = new_status
        now = datetime.now(UTC)
        if new_status in (WatchStatus.DONE, WatchStatus.CANCELLED):
            item.completed_at = now
        elif new_status == WatchStatus.DIE:
            item.live_detected_at = None
            item.completed_at = None
        await self._repo.update(item, previous_status=prev)
        await self._repo.append_event(
            WatchEvent(
                watch_id=item.id,
                chat_id=item.chat_id,
                uid=item.uid,
                event_type=new_status.value,
                detail=f"{prev.value}->{new_status.value}",
            )
        )
        return item

    async def recheck_one(self, chat_id: int, watch_id: int) -> tuple[WatchItem, CheckOutcome]:
        item = await self._require_owned(chat_id, watch_id)
        res = await self._checker.check(item.uid)
        now = datetime.now(UTC)
        item.last_checked_at = now
        prev = item.status
        if res.outcome == CheckOutcome.LIVE and item.status == WatchStatus.DIE:
            item.status = WatchStatus.LIVE
            item.live_detected_at = now
            await self._repo.update(item, previous_status=prev)
            await self._repo.append_event(
                WatchEvent(
                    watch_id=item.id,
                    chat_id=item.chat_id,
                    uid=item.uid,
                    event_type="LIVE_DETECTED",
                    detail="manual_button",
                )
            )
        else:
            await self._repo.update(item)
        return item, res.outcome


class WatcherScanService:
    def __init__(
        self,
        *,
        repo: RedisWatchRepository,
        checker: FacebookCheckerPort,
        cache: CachePort,
        notifier: WatchNotifierPort,
        concurrency: int = 50,
        cache_ttl_seconds: int = 600,
    ) -> None:
        self._repo = repo
        self._checker = checker
        self._cache = cache
        self._notifier = notifier
        self._concurrency = max(1, concurrency)
        self._cache_ttl = cache_ttl_seconds

    async def scan_once(self) -> int:
        items = await self._repo.list_die_items()
        if not items:
            return 0

        sem = asyncio.Semaphore(self._concurrency)

        async def _scan_item(item: WatchItem) -> bool:
            async with sem:
                res = await self._checker.check(item.uid)
                if res.outcome == CheckOutcome.ERROR:
                    return False
                now = datetime.now(UTC)
                item.last_checked_at = now
                if res.outcome == CheckOutcome.LIVE:
                    locked = await self._cache.acquire_lock(f"notify:{item.id}", ttl_seconds=300)
                    if not locked:
                        return False
                    item.status = WatchStatus.LIVE
                    item.live_detected_at = now
                    await self._repo.update(item, previous_status=WatchStatus.DIE)
                    await self._cache.set_cached_outcome(
                        item.uid,
                        CheckOutcome.LIVE,
                        self._cache_ttl,
                    )
                    await self._repo.append_event(
                        WatchEvent(
                            watch_id=item.id,
                            chat_id=item.chat_id,
                            uid=item.uid,
                            event_type="LIVE_DETECTED",
                            detail="cron",
                        )
                    )
                    await self._notifier.notify_live(item)
                    return True
                await self._repo.update(item)
                return False

        results = await asyncio.gather(*(_scan_item(i) for i in items))
        notified = sum(1 for ok in results if ok)
        if notified > 0:
            logger.info("watcher_scan_notified", count=notified, scanned=len(items))
        return notified
