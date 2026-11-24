from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import structlog
from redis.asyncio import Redis

from src.core.ports import CheckOutcome, FacebookCheckerPort, LockPort
from src.modules.watch.domain import (
    WatchItem,
    WatchStats,
    WatchStatus,
    parse_add_line,
)

logger = structlog.get_logger(__name__)


class DuplicateWatchError(ValueError):
    def __init__(self, existing: WatchItem) -> None:
        self.existing = existing
        super().__init__(
            f"UID <code>{existing.uid}</code> đã tồn tại ở kèo <b>#{existing.id}</b> "
            f"({existing.status.value})."
        )


class WatchNotifierPort(Protocol):
    async def notify_live(self, item: WatchItem) -> None: ...


class RedisWatchRepository:
    def __init__(
        self,
        redis: Redis,
        prefix: str = "checklivebot",
        ttl_days: int = 90,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._ttl_days = max(1, ttl_days)
        self._ttl_seconds = self._ttl_days * 86400

    def _k(self, suffix: str) -> str:
        return f"{self._prefix}:{suffix}"

    def _cutoff_ts(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - timedelta(days=self._ttl_days)).timestamp()

    def _remaining_ttl(self, created_at: datetime, now: datetime | None = None) -> int:
        ref_now = now or datetime.now(UTC)
        aware_created = (
            created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
        )
        return max(
            1,
            int((aware_created + timedelta(days=self._ttl_days) - ref_now).total_seconds()),
        )

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
        seq_key = self._k("seq:watch_id")
        watch_id = int(await self._redis.incr(seq_key))
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
        chat_key = self._k(f"watches:chat:{chat_id}")
        status_key = self._k(f"watches:chat:{chat_id}:status:{status.value}")
        uid_key = self._k(f"chat_uid:{chat_id}")
        die_key = self._k("watches:die")
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.expire(seq_key, self._ttl_seconds)
            pipe.set(self._k(f"watch:{watch_id}"), item.model_dump_json(), ex=self._ttl_seconds)
            pipe.zadd(chat_key, {str(watch_id): score})
            pipe.expire(chat_key, self._ttl_seconds)
            pipe.zadd(status_key, {str(watch_id): score})
            pipe.expire(status_key, self._ttl_seconds)
            pipe.hset(uid_key, uid, str(watch_id))
            pipe.expire(uid_key, self._ttl_seconds)
            if status == WatchStatus.DIE:
                pipe.sadd(die_key, str(watch_id))
                pipe.expire(die_key, self._ttl_seconds)
            await pipe.execute()
        return item

    async def find_by_uid(self, chat_id: int, uid: str) -> WatchItem | None:
        raw_id = cast(str | None, await self._redis.hget(self._k(f"chat_uid:{chat_id}"), uid))  # type: ignore[misc]
        if not raw_id:
            return None
        item = await self.get(int(raw_id))
        if item is not None and item.chat_id == chat_id and item.uid == uid:
            return item
        await self._redis.hdel(self._k(f"chat_uid:{chat_id}"), uid)  # type: ignore[misc]
        return None

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
        item_ttl = self._remaining_ttl(item.created_at)
        chat_key = self._k(f"watches:chat:{item.chat_id}")
        status_key = self._k(f"watches:chat:{item.chat_id}:status:{item.status.value}")
        uid_key = self._k(f"chat_uid:{item.chat_id}")
        die_key = self._k("watches:die")
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._k(f"watch:{item.id}"), item.model_dump_json(), ex=item_ttl)
            pipe.hset(uid_key, item.uid, str(item.id))
            pipe.expire(uid_key, self._ttl_seconds)
            pipe.expire(chat_key, self._ttl_seconds)
            if previous_status is not None and previous_status != item.status:
                pipe.zrem(
                    self._k(f"watches:chat:{item.chat_id}:status:{previous_status.value}"),
                    str(item.id),
                )
                pipe.zadd(status_key, {str(item.id): score})
            pipe.expire(status_key, self._ttl_seconds)
            if item.status == WatchStatus.DIE:
                pipe.sadd(die_key, str(item.id))
                pipe.expire(die_key, self._ttl_seconds)
            else:
                pipe.srem(die_key, str(item.id))
            await pipe.execute()
        return item

    async def delete(self, item: WatchItem) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._k(f"watch:{item.id}"))
            pipe.zrem(self._k(f"watches:chat:{item.chat_id}"), str(item.id))
            pipe.zrem(
                self._k(f"watches:chat:{item.chat_id}:status:{item.status.value}"),
                str(item.id),
            )
            pipe.srem(self._k("watches:die"), str(item.id))
            pipe.hdel(self._k(f"chat_uid:{item.chat_id}"), item.uid)
            await pipe.execute()

    async def count_active_die(self, chat_id: int, *, now: datetime | None = None) -> int:
        key = self._k(f"watches:chat:{chat_id}:status:DIE")
        cutoff = self._cutoff_ts(now)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.zremrangebyscore(key, "-inf", cutoff)
            pipe.zcard(key)
            _, count = await pipe.execute()
        return int(count)

    async def list_die_items(self) -> list[WatchItem]:
        die_key = self._k("watches:die")
        ids = list(cast(set[str], await self._redis.smembers(die_key)))  # type: ignore[misc]
        if not ids:
            return []
        raws = await self._redis.mget([self._k(f"watch:{wid}") for wid in ids])
        items: list[WatchItem] = []
        stale_ids: list[str] = []
        for wid, raw in zip(ids, raws, strict=True):
            if not raw:
                stale_ids.append(wid)
                continue
            item = WatchItem.model_validate_json(raw)
            if item.status == WatchStatus.DIE:
                items.append(item)
            else:
                stale_ids.append(wid)
        if stale_ids:
            await self._redis.srem(die_key, *stale_ids)  # type: ignore[misc]
        return items

    async def list_chat_items(
        self,
        chat_id: int,
        *,
        status_filter: WatchStatus | None = None,
        page: int = 1,
        page_size: int = 10,
        now: datetime | None = None,
    ) -> tuple[list[WatchItem], int, int]:
        key = (
            self._k(f"watches:chat:{chat_id}:status:{status_filter.value}")
            if status_filter
            else self._k(f"watches:chat:{chat_id}")
        )
        cutoff = self._cutoff_ts(now)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.zremrangebyscore(key, "-inf", cutoff)
            pipe.zcard(key)
            _, total_raw = await pipe.execute()
        total = int(total_raw)
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

    async def get_stats(self, chat_id: int, *, now: datetime | None = None) -> WatchStats:
        cutoff = self._cutoff_ts(now)
        keys = (
            self._k(f"watches:chat:{chat_id}"),
            self._k(f"watches:chat:{chat_id}:status:DIE"),
            self._k(f"watches:chat:{chat_id}:status:LIVE"),
            self._k(f"watches:chat:{chat_id}:status:DONE"),
            self._k(f"watches:chat:{chat_id}:status:CANCELLED"),
        )
        async with self._redis.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.zremrangebyscore(key, "-inf", cutoff)
            for key in keys:
                pipe.zcard(key)
            results = await pipe.execute()
        total, die, live, done, cancelled = results[len(keys) :]
        return WatchStats(
            total=int(total),
            die=int(die),
            live=int(live),
            done=int(done),
            cancelled=int(cancelled),
        )

    async def get_chat_hidden(self, chat_id: int) -> bool:
        raw = await self._redis.get(self._k(f"pref:hidden:{chat_id}"))
        return raw == "1"

    async def set_chat_hidden(self, chat_id: int, hidden: bool) -> None:
        key = self._k(f"pref:hidden:{chat_id}")
        if hidden:
            await self._redis.set(key, "1", ex=self._ttl_seconds)
        else:
            await self._redis.delete(key)


class WatchService:
    def __init__(
        self,
        *,
        repo: RedisWatchRepository,
        checker: FacebookCheckerPort,
        lock: LockPort | None = None,
        max_active_per_chat: int = 100,
        concurrency: int = 20,
    ) -> None:
        self._repo = repo
        self._checker = checker
        self._lock = lock
        self._max_active_per_chat = max_active_per_chat
        self._concurrency = max(1, concurrency)

    async def find_by_uid(self, chat_id: int, uid: str) -> WatchItem | None:
        item = await self._repo.find_by_uid(chat_id, uid)
        if item is not None:
            item.is_hidden = await self._repo.get_chat_hidden(chat_id)
        return item

    async def add_watch(self, chat_id: int, user_id: int, raw_args: str) -> WatchItem:
        parsed = parse_add_line(raw_args)

        async def _count_die() -> int:
            if self._max_active_per_chat > 0:
                return await self._repo.count_active_die(chat_id)
            return 0

        # Run Redis pre-checks and live Facebook check concurrently to cut latency
        existing, active_count, is_hidden, res = await asyncio.gather(
            self._repo.find_by_uid(chat_id, parsed.uid),
            _count_die(),
            self._repo.get_chat_hidden(chat_id),
            self._checker.check(parsed.uid),
        )
        if existing is not None:
            existing.is_hidden = is_hidden
            raise DuplicateWatchError(existing)

        if self._max_active_per_chat > 0 and active_count >= self._max_active_per_chat:
            raise ValueError(
                f"Chat đã đạt giới hạn {self._max_active_per_chat} kèo DIE đang theo dõi."
            )

        initial_status = WatchStatus.LIVE if res.outcome == CheckOutcome.LIVE else WatchStatus.DIE
        item = await self._repo.create(
            chat_id=chat_id,
            owner_user_id=user_id,
            uid=parsed.uid,
            note=parsed.note,
            price=parsed.price,
            faq=parsed.faq,
            status=initial_status,
        )
        item.is_hidden = is_hidden
        return item

    async def get_watch(self, chat_id: int, watch_id: int) -> WatchItem:
        return await self._require_owned(chat_id, watch_id)

    async def delete_watch(self, chat_id: int, watch_id: int) -> WatchItem:
        item = await self._require_owned(chat_id, watch_id)
        await self._repo.delete(item)
        return item

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
            page_size=10,
        )

    async def get_stats(self, chat_id: int) -> WatchStats:
        return await self._repo.get_stats(chat_id)

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
                    verified = await self._checker.verify_live(item.uid)
                    if verified.outcome != CheckOutcome.LIVE:
                        return
                    if self._lock is not None:
                        locked = await self._lock.acquire_lock(
                            f"notify:{item.id}",
                            ttl_seconds=300,
                        )
                        if not locked:
                            return
                    item.status = WatchStatus.LIVE
                    item.live_detected_at = now
                    await self._repo.update(item, previous_status=WatchStatus.DIE)
                    turned_live.append(item)

        await asyncio.gather(*(_check_item(i) for i in items))
        if turned_live:
            is_hidden = await self._repo.get_chat_hidden(chat_id)
            for item in turned_live:
                item.is_hidden = is_hidden
        return len(items), turned_live

    async def _require_owned(self, chat_id: int, watch_id: int) -> WatchItem:
        item = await self._repo.get(watch_id)
        if item is None or item.chat_id != chat_id:
            raise ValueError("Không tìm thấy kèo trong chat này.")
        item.is_hidden = await self._repo.get_chat_hidden(chat_id)
        return item

    async def set_hidden(self, chat_id: int, watch_id: int, hidden: bool) -> WatchItem:
        await self._repo.set_chat_hidden(chat_id, hidden)
        item = await self._require_owned(chat_id, watch_id)
        item.is_hidden = hidden
        return item

    async def update_watch(
        self,
        chat_id: int,
        watch_id: int,
        *,
        note: str,
        price: str,
        faq: str,
    ) -> WatchItem:
        item = await self._require_owned(chat_id, watch_id)
        item.note = note.strip() or "Theo dõi nhanh"
        item.price = price.strip() or "0"
        cleaned_faq = faq.strip()
        item.faq = "" if cleaned_faq == "-" else cleaned_faq
        is_hidden = item.is_hidden
        updated = await self._repo.update(item)
        updated.is_hidden = is_hidden
        return updated

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
        return await self._repo.update(item, previous_status=prev)


class WatcherScanService:
    def __init__(
        self,
        *,
        repo: RedisWatchRepository,
        checker: FacebookCheckerPort,
        lock: LockPort,
        notifier: WatchNotifierPort,
        concurrency: int = 50,
    ) -> None:
        self._repo = repo
        self._checker = checker
        self._lock = lock
        self._notifier = notifier
        self._concurrency = max(1, concurrency)

    async def scan_once(self) -> int:
        items = await self._repo.list_die_items()
        if not items:
            return 0

        # Deduplicate by UID so multiple chats watching the same UID only trigger 1 check
        grouped: dict[str, list[WatchItem]] = {}
        for item in items:
            grouped.setdefault(item.uid, []).append(item)

        sem = asyncio.Semaphore(self._concurrency)

        async def _scan_uid(uid: str, uid_items: list[WatchItem]) -> int:
            try:
                async with sem:
                    res = await self._checker.check(uid)
                    if res.outcome != CheckOutcome.LIVE:
                        # Skip redundant Redis writes when item is still DIE or ERROR
                        return 0

                    # Layer-2 double verification to guarantee 0% false LIVE alerts
                    verified = await self._checker.verify_live(uid)
                    if verified.outcome != CheckOutcome.LIVE:
                        logger.info(
                            "watcher_double_verify_rejected",
                            uid=uid,
                            watch_ids=[it.id for it in uid_items],
                            second_outcome=verified.outcome.value,
                        )
                        return 0

                    now = datetime.now(UTC)
                    count = 0
                    for item in uid_items:
                        lock_key = f"notify:{item.id}"
                        locked = await self._lock.acquire_lock(lock_key, ttl_seconds=300)
                        if not locked:
                            continue
                        item.last_checked_at = now
                        item.status = WatchStatus.LIVE
                        item.live_detected_at = now
                        item.is_hidden = await self._repo.get_chat_hidden(item.chat_id)
                        try:
                            await self._notifier.notify_live(item)
                        except Exception as exc:
                            logger.warning(
                                "watcher_notify_failed",
                                watch_id=item.id,
                                chat_id=item.chat_id,
                                error=str(exc),
                            )
                            await self._lock.release_lock(lock_key)
                            continue
                        await self._repo.update(item, previous_status=WatchStatus.DIE)
                        count += 1
                    return count
            except Exception as exc:
                logger.warning("watcher_scan_uid_error", uid=uid, error=str(exc))
                return 0

        results = await asyncio.gather(
            *(_scan_uid(uid, uid_items) for uid, uid_items in grouped.items())
        )
        notified = sum(results)
        if notified > 0:
            logger.info(
                "watcher_scan_notified",
                count=notified,
                scanned=len(items),
                unique_uids=len(grouped),
            )
        return notified
