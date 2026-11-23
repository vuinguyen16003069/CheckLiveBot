from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

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

    @staticmethod
    def _expiry_member(watch_id: int, chat_id: int, status: WatchStatus, uid: str) -> str:
        return f"{watch_id}:{chat_id}:{status.value}:{uid}"

    def _all_expiry_members(self, watch_id: int, chat_id: int, uid: str) -> list[str]:
        return [self._expiry_member(watch_id, chat_id, st, uid) for st in WatchStatus]

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
        expires_at = (now + timedelta(days=self._ttl_days)).timestamp()
        chat_key = self._k(f"watches:chat:{chat_id}")
        status_key = self._k(f"watches:chat:{chat_id}:status:{status.value}")
        uid_key = self._k(f"chat_uid:{chat_id}")
        die_key = self._k("watches:die")
        expiry_key = self._k("watches:expiry")
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
            pipe.zadd(expiry_key, {self._expiry_member(watch_id, chat_id, status, uid): expires_at})
            pipe.expire(expiry_key, self._ttl_seconds)
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
        ref_time = (
            item.completed_at or item.live_detected_at or item.last_checked_at or item.created_at
        )
        expires_at = (ref_time + timedelta(days=self._ttl_days)).timestamp()
        chat_key = self._k(f"watches:chat:{item.chat_id}")
        status_key = self._k(f"watches:chat:{item.chat_id}:status:{item.status.value}")
        uid_key = self._k(f"chat_uid:{item.chat_id}")
        die_key = self._k("watches:die")
        expiry_key = self._k("watches:expiry")
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._k(f"watch:{item.id}"), item.model_dump_json(), ex=self._ttl_seconds)
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

            pipe.zrem(expiry_key, *self._all_expiry_members(item.id, item.chat_id, item.uid))
            pipe.zadd(
                expiry_key,
                {self._expiry_member(item.id, item.chat_id, item.status, item.uid): expires_at},
            )
            pipe.expire(expiry_key, self._ttl_seconds)
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
            pipe.zrem(
                self._k("watches:expiry"),
                *self._all_expiry_members(item.id, item.chat_id, item.uid),
            )
            await pipe.execute()

    async def count_active_die(self, chat_id: int) -> int:
        return int(await self._redis.zcard(self._k(f"watches:chat:{chat_id}:status:DIE")))

    async def list_die_items(self) -> list[WatchItem]:
        ids = cast(set[str], await self._redis.smembers(self._k("watches:die")))  # type: ignore[misc]
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
        page_size: int = 10,
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

    async def get_chat_hidden(self, chat_id: int) -> bool:
        raw = await self._redis.get(self._k(f"pref:hidden:{chat_id}"))
        return raw == "1"

    async def set_chat_hidden(self, chat_id: int, hidden: bool) -> None:
        key = self._k(f"pref:hidden:{chat_id}")
        if hidden:
            await self._redis.set(key, "1", ex=self._ttl_seconds)
        else:
            await self._redis.delete(key)

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """Prune all watch entries older than ttl_days (default 90 days / 3 months)."""
        cutoff = (now or datetime.now(UTC)).timestamp()
        expiry_key = self._k("watches:expiry")
        members = await self._redis.zrangebyscore(expiry_key, "-inf", cutoff)
        if not members:
            return 0

        cleaned = 0
        async with self._redis.pipeline(transaction=True) as pipe:
            for member in members:
                parts = str(member).split(":", 3)
                pipe.zrem(expiry_key, member)
                if len(parts) != 4:
                    continue
                watch_id, chat_id, status_val, uid = parts
                pipe.delete(self._k(f"watch:{watch_id}"))
                pipe.zrem(self._k(f"watches:chat:{chat_id}"), watch_id)
                pipe.zrem(self._k(f"watches:chat:{chat_id}:status:{status_val}"), watch_id)
                pipe.srem(self._k("watches:die"), watch_id)
                pipe.hdel(self._k(f"chat_uid:{chat_id}"), uid)
                cleaned += 1
            await pipe.execute()

        if cleaned > 0:
            logger.info("watch_ttl_cleanup_completed", cleaned=cleaned, ttl_days=self._ttl_days)
        return cleaned


class WatchService:
    def __init__(
        self,
        *,
        repo: RedisWatchRepository,
        checker: FacebookCheckerPort,
        max_active_per_chat: int = 100,
        concurrency: int = 20,
    ) -> None:
        self._repo = repo
        self._checker = checker
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
                    item.status = WatchStatus.LIVE
                    item.live_detected_at = now
                    await self._repo.update(item, previous_status=WatchStatus.DIE)
                    turned_live.append(item)
                elif res.outcome == CheckOutcome.DIE:
                    await self._repo.update(item)

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

    async def update_watch_field(
        self,
        chat_id: int,
        watch_id: int,
        field: Literal["note", "price", "faq"],
        value: str,
    ) -> WatchItem:
        item = await self._require_owned(chat_id, watch_id)
        cleaned = value.strip()
        if field == "note":
            item.note = cleaned or "Theo dõi nhanh"
        elif field == "price":
            item.price = cleaned or "0"
        elif field == "faq":
            item.faq = "" if cleaned == "-" else cleaned
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
                    locked = await self._lock.acquire_lock(f"notify:{item.id}", ttl_seconds=300)
                    if not locked:
                        continue
                    item.last_checked_at = now
                    item.status = WatchStatus.LIVE
                    item.live_detected_at = now
                    await self._repo.update(item, previous_status=WatchStatus.DIE)
                    item.is_hidden = await self._repo.get_chat_hidden(item.chat_id)
                    await self._notifier.notify_live(item)
                    count += 1
                return count

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
