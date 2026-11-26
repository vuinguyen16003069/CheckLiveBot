from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from redis.asyncio import Redis

from src.core.ports import CheckOutcome, FacebookCheckerPort, LockPort
from src.modules.bulk.domain import extract_fb_url, extract_uid
from src.modules.watch.domain import (
    WatchItem,
    WatchStats,
    WatchStatus,
    normalize_watch_fields,
    parse_add_line,
)

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class WatchTtlPolicy:
    """Single source of truth for tiered Redis TTLs across watch lifecycle & metadata."""

    die_days: int = 90
    live_days: int = 60
    done_days: int = 30
    cancelled_days: int = 14
    pref_days: int = 365
    seq_days: int = 0  # 0 = permanent (no TTL)

    def __post_init__(self) -> None:
        for field_name in ("die_days", "live_days", "done_days", "cancelled_days", "pref_days"):
            if getattr(self, field_name) < 1:
                raise ValueError(f"{field_name} must be >= 1")
        if self.seq_days < 0:
            raise ValueError("seq_days must be >= 0")

    def days_for_status(self, status: WatchStatus) -> int:
        if status == WatchStatus.DIE:
            return self.die_days
        if status == WatchStatus.LIVE:
            return self.live_days
        if status == WatchStatus.DONE:
            return self.done_days
        return self.cancelled_days

    def seconds_for_status(self, status: WatchStatus) -> int:
        return self.days_for_status(status) * 86400

    @property
    def max_watch_days(self) -> int:
        return max(self.die_days, self.live_days, self.done_days, self.cancelled_days)

    @property
    def max_watch_ttl_seconds(self) -> int:
        return self.max_watch_days * 86400

    @property
    def die_ttl_seconds(self) -> int:
        return self.die_days * 86400

    @property
    def pref_ttl_seconds(self) -> int:
        return self.pref_days * 86400

    @property
    def seq_ttl_seconds(self) -> int:
        return self.seq_days * 86400

    def anchor_time(self, item: WatchItem) -> datetime:
        if item.status == WatchStatus.DIE:
            return _ensure_utc(item.last_checked_at)
        if item.status == WatchStatus.LIVE:
            return _ensure_utc(item.live_detected_at or item.created_at)
        return _ensure_utc(item.completed_at or item.created_at)

    def remaining_ttl(self, item: WatchItem, now: datetime | None = None) -> int:
        ref_now = _ensure_utc(now or datetime.now(UTC))
        anchor = self.anchor_time(item)
        days = self.days_for_status(item.status)
        return max(1, int((anchor + timedelta(days=days) - ref_now).total_seconds()))

    def expiry_ts(self, item: WatchItem) -> float:
        anchor = self.anchor_time(item)
        days = self.days_for_status(item.status)
        return (anchor + timedelta(days=days)).timestamp()

    def cutoff_ts(
        self,
        status: WatchStatus | None = None,
        now: datetime | None = None,
    ) -> float:
        ref_now = _ensure_utc(now or datetime.now(UTC))
        days = self.days_for_status(status) if status is not None else self.max_watch_days
        return (ref_now - timedelta(days=days)).timestamp()


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
        ttl_policy: WatchTtlPolicy | None = None,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._policy = ttl_policy or WatchTtlPolicy()

    @property
    def ttl_policy(self) -> WatchTtlPolicy:
        return self._policy

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
        fb_name: str = "",
        avatar_url: str = "",
    ) -> WatchItem:
        seq_key = self._k("seq:watch_id")
        watch_id = await self._redis.incr(seq_key)
        now = datetime.now(UTC)
        item = WatchItem(
            id=watch_id,
            chat_id=chat_id,
            owner_user_id=owner_user_id,
            uid=uid,
            fb_name=fb_name,
            avatar_url=avatar_url,
            note=note,
            price=price,
            faq=faq,
            status=status,
            created_at=now,
            last_checked_at=now,
            live_detected_at=now if status == WatchStatus.LIVE else None,
            completed_at=now if status in (WatchStatus.DONE, WatchStatus.CANCELLED) else None,
        )
        status_score = self._policy.anchor_time(item).timestamp()
        item_ttl = self._policy.seconds_for_status(status)
        container_ttl = self._policy.max_watch_ttl_seconds
        chat_key = self._k(f"watches:chat:{chat_id}")
        status_key = self._k(f"watches:chat:{chat_id}:status:{status.value}")
        uid_key = self._k(f"chat_uid:{chat_id}")
        die_key = self._k("watches:die")
        async with self._redis.pipeline(transaction=True) as pipe:
            if self._policy.seq_ttl_seconds > 0:
                pipe.expire(seq_key, self._policy.seq_ttl_seconds)
            pipe.set(self._k(f"watch:{watch_id}"), item.model_dump_json(), ex=item_ttl)
            pipe.zadd(chat_key, {str(watch_id): status_score})
            pipe.expire(chat_key, container_ttl)
            pipe.zadd(status_key, {str(watch_id): status_score})
            pipe.expire(status_key, item_ttl)
            pipe.hset(uid_key, uid, str(watch_id))
            pipe.expire(uid_key, container_ttl)
            if status == WatchStatus.DIE:
                pipe.sadd(die_key, str(watch_id))
                pipe.expire(die_key, self._policy.die_ttl_seconds)
            await pipe.execute()
        return item

    async def find_by_uid(self, chat_id: int, uid: str) -> WatchItem | None:
        raw_id = cast(str | None, await self._redis.hget(self._k(f"chat_uid:{chat_id}"), uid))
        if not raw_id:
            return None
        item = await self.get(int(raw_id))
        if item is not None and item.chat_id == chat_id and item.uid == uid:
            return item
        await self._redis.hdel(self._k(f"chat_uid:{chat_id}"), uid)
        return None

    async def get(self, watch_id: int) -> WatchItem | None:
        raw = await self._redis.get(self._k(f"watch:{watch_id}"))
        if not raw:
            return None
        return WatchItem.model_validate_json(raw)

    async def get_with_hidden(self, chat_id: int, watch_id: int) -> tuple[WatchItem | None, bool]:
        raw_item, raw_hidden = await self._redis.mget(
            [self._k(f"watch:{watch_id}"), self._k(f"pref:hidden:{chat_id}")]
        )
        if not raw_item:
            return None, raw_hidden == "1"
        return WatchItem.model_validate_json(raw_item), raw_hidden == "1"

    async def find_by_uid_with_hidden(self, chat_id: int, uid: str) -> WatchItem | None:
        raw_id = cast(str | None, await self._redis.hget(self._k(f"chat_uid:{chat_id}"), uid))
        if not raw_id:
            return None
        item, is_hidden = await self.get_with_hidden(chat_id, int(raw_id))
        if item is not None and item.chat_id == chat_id and item.uid == uid:
            item.is_hidden = is_hidden
            return item
        await self._redis.hdel(self._k(f"chat_uid:{chat_id}"), uid)
        return None

    async def update(
        self,
        item: WatchItem,
        *,
        previous_status: WatchStatus | None = None,
    ) -> WatchItem:
        item_ttl = self._policy.remaining_ttl(item)
        status_ttl = self._policy.seconds_for_status(item.status)
        status_score = self._policy.anchor_time(item).timestamp()
        container_ttl = self._policy.max_watch_ttl_seconds
        chat_key = self._k(f"watches:chat:{item.chat_id}")
        status_key = self._k(f"watches:chat:{item.chat_id}:status:{item.status.value}")
        uid_key = self._k(f"chat_uid:{item.chat_id}")
        die_key = self._k("watches:die")
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._k(f"watch:{item.id}"), item.model_dump_json(), ex=item_ttl)
            pipe.hset(uid_key, item.uid, str(item.id))
            pipe.expire(uid_key, container_ttl)
            pipe.zadd(chat_key, {str(item.id): status_score})
            pipe.expire(chat_key, container_ttl)
            if previous_status is not None and previous_status != item.status:
                pipe.zrem(
                    self._k(f"watches:chat:{item.chat_id}:status:{previous_status.value}"),
                    str(item.id),
                )
            if previous_status is not None:
                pipe.zadd(status_key, {str(item.id): status_score})
            pipe.expire(status_key, status_ttl)
            if item.status == WatchStatus.DIE:
                pipe.sadd(die_key, str(item.id))
                pipe.expire(die_key, self._policy.die_ttl_seconds)
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
        cutoff = self._policy.cutoff_ts(WatchStatus.DIE, now)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.zremrangebyscore(key, "-inf", cutoff)
            pipe.zcard(key)
            _, count = await pipe.execute()
        return int(count)

    async def list_die_items(self) -> list[WatchItem]:
        die_key = self._k("watches:die")
        ids = list(cast(set[str], await self._redis.smembers(die_key)))
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
            await self._redis.srem(die_key, *stale_ids)
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
        cutoff = self._policy.cutoff_ts(status_filter, now)
        if page <= 1:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.zremrangebyscore(key, "-inf", cutoff)
                pipe.zcard(key)
                pipe.zrevrange(key, 0, page_size - 1)
                _, total_raw, ids = await pipe.execute()
            total = int(total_raw)
            total_pages = max(1, math.ceil(total / page_size))
        else:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.zremrangebyscore(key, "-inf", cutoff)
                pipe.zcard(key)
                _, total_raw = await pipe.execute()
            total = int(total_raw)
            total_pages = max(1, math.ceil(total / page_size))
            safe_page = min(page, total_pages)
            start = (safe_page - 1) * page_size
            end = start + page_size - 1
            ids = await self._redis.zrevrange(key, start, end)
        if not ids:
            return [], total, total_pages
        raws = await self._redis.mget([self._k(f"watch:{wid}") for wid in ids])
        items: list[WatchItem] = []
        stale_ids: list[str] = []
        for wid, raw in zip(ids, raws, strict=True):
            if raw:
                items.append(WatchItem.model_validate_json(raw))
            else:
                stale_ids.append(str(wid))
        if stale_ids:
            await self._redis.zrem(key, *stale_ids)
            total = max(len(items), total - len(stale_ids))
            total_pages = max(1, math.ceil(total / page_size))
        return items, total, total_pages

    async def compact_chat_uid(self, chat_id: int) -> int:
        """Remove dead uid -> watch_id entries whose watch:{id} key has expired."""
        uid_key = self._k(f"chat_uid:{chat_id}")
        mapping = cast(dict[str, str], await self._redis.hgetall(uid_key))
        if not mapping:
            return 0
        raws = await self._redis.mget([self._k(f"watch:{wid}") for wid in mapping.values()])
        stale_uids = [uid for (uid, _), raw in zip(mapping.items(), raws, strict=True) if not raw]
        if stale_uids:
            await self._redis.hdel(uid_key, *stale_uids)
        return len(stale_uids)

    async def get_stats(self, chat_id: int, *, now: datetime | None = None) -> WatchStats:
        statuses = (
            WatchStatus.DIE,
            WatchStatus.LIVE,
            WatchStatus.DONE,
            WatchStatus.CANCELLED,
        )
        status_keys = tuple(
            self._k(f"watches:chat:{chat_id}:status:{status.value}") for status in statuses
        )
        uid_key = self._k(f"chat_uid:{chat_id}")
        async with self._redis.pipeline(transaction=False) as pipe:
            for status, key in zip(statuses, status_keys, strict=True):
                pipe.zremrangebyscore(key, "-inf", self._policy.cutoff_ts(status, now))
            for key in status_keys:
                pipe.zcard(key)
            pipe.hlen(uid_key)
            results = await pipe.execute()
        die, live, done, cancelled = (int(v) for v in results[len(status_keys) : -1])
        uid_count = int(results[-1])
        total = die + live + done + cancelled
        if uid_count > max(50, total * 2):
            await self.compact_chat_uid(chat_id)
        return WatchStats(
            total=total,
            die=die,
            live=live,
            done=done,
            cancelled=cancelled,
        )

    async def get_chat_hidden(self, chat_id: int) -> bool:
        raw = await self._redis.get(self._k(f"pref:hidden:{chat_id}"))
        return raw == "1"

    async def set_chat_hidden(self, chat_id: int, hidden: bool) -> None:
        key = self._k(f"pref:hidden:{chat_id}")
        if hidden:
            await self._redis.set(key, "1", ex=self._policy.pref_ttl_seconds)
        else:
            await self._redis.delete(key)

    async def set_chat_hidden_and_get(
        self, chat_id: int, watch_id: int, hidden: bool
    ) -> WatchItem | None:
        pref_key = self._k(f"pref:hidden:{chat_id}")
        watch_key = self._k(f"watch:{watch_id}")
        async with self._redis.pipeline(transaction=False) as pipe:
            if hidden:
                pipe.set(pref_key, "1", ex=self._policy.pref_ttl_seconds)
            else:
                pipe.delete(pref_key)
            pipe.get(watch_key)
            _, raw_item = await pipe.execute()
        if not raw_item:
            return None
        return WatchItem.model_validate_json(raw_item)


class WatchService:
    def __init__(
        self,
        *,
        repo: RedisWatchRepository,
        checker: FacebookCheckerPort,
        lock: LockPort | None = None,
        max_active_per_chat: int = 100,
        concurrency: int = 20,
        notify_lock_ttl_seconds: int = 300,
    ) -> None:
        self._repo = repo
        self._checker = checker
        self._lock = lock
        self._max_active_per_chat = max_active_per_chat
        self._concurrency = max(1, concurrency)
        self._notify_lock_ttl_seconds = max(30, notify_lock_ttl_seconds)

    async def resolve_input_uid(self, raw: str) -> str | None:
        uid = extract_uid(raw)
        if uid:
            return uid
        fb_url = extract_fb_url(raw)
        if fb_url:
            return await self._checker.resolve_uid_from_url(fb_url)
        return None

    async def find_by_uid(self, chat_id: int, uid: str) -> WatchItem | None:
        return await self._repo.find_by_uid_with_hidden(chat_id, uid)

    async def add_watch(self, chat_id: int, user_id: int, raw_args: str) -> WatchItem:
        first_part = raw_args.split("|", 1)[0].strip()
        if first_part and not extract_uid(first_part):
            fb_url = extract_fb_url(first_part)
            if fb_url:
                resolved = await self._checker.resolve_uid_from_url(fb_url)
                if not resolved:
                    raise ValueError(
                        "Không lấy được UID từ link Facebook này — tài khoản có thể đã DIE, "
                        "bị khóa hoặc để chế độ riêng tư.\n"
                        "👉 Vui lòng nhập trực tiếp UID số (1–20 chữ số)."
                    )
                rest = raw_args.split("|", 1)[1] if "|" in raw_args else ""
                raw_args = f"{resolved} | {rest}" if rest else resolved
        parsed = parse_add_line(raw_args)
        add_lock_key = f"add:{chat_id}:{parsed.uid}"
        lock_acquired = False
        if self._lock is not None:
            lock_acquired = await self._lock.acquire_lock(add_lock_key, ttl_seconds=15)
            if not lock_acquired:
                existing_now = await self._repo.find_by_uid_with_hidden(chat_id, parsed.uid)
                if existing_now is not None:
                    raise DuplicateWatchError(existing_now)
                raise ValueError("UID này đang được xử lý, vui lòng thử lại sau giây lát.")

        async def _count_die() -> int:
            if self._max_active_per_chat > 0:
                return await self._repo.count_active_die(chat_id)
            return 0

        # Run Redis pre-checks, live Facebook check, and profile head stream concurrently
        profile_task = asyncio.create_task(self._checker.fetch_profile_info(parsed.uid))
        try:
            existing, active_count, is_hidden, res = await asyncio.gather(
                self._repo.find_by_uid(chat_id, parsed.uid),
                _count_die(),
                self._repo.get_chat_hidden(chat_id),
                self._checker.check(parsed.uid),
            )
            if existing is not None:
                profile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await profile_task
                existing.is_hidden = is_hidden
                raise DuplicateWatchError(existing)

            if self._max_active_per_chat > 0 and active_count >= self._max_active_per_chat:
                profile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await profile_task
                raise ValueError(
                    f"Chat đã đạt giới hạn {self._max_active_per_chat} kèo DIE đang theo dõi."
                )

            initial_status = (
                WatchStatus.LIVE if res.outcome == CheckOutcome.LIVE else WatchStatus.DIE
            )
            fb_name = ""
            avatar_url = ""
            if initial_status == WatchStatus.LIVE:
                fetched_name, fetched_avatar = await profile_task
                fb_name = fetched_name or ""
                avatar_url = fetched_avatar or ""
            else:
                profile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await profile_task

            item = await self._repo.create(
                chat_id=chat_id,
                owner_user_id=user_id,
                uid=parsed.uid,
                fb_name=fb_name,
                avatar_url=avatar_url,
                note=parsed.note,
                price=parsed.price,
                faq=parsed.faq,
                status=initial_status,
            )
            item.is_hidden = is_hidden
            return item
        except BaseException:
            if not profile_task.done():
                profile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await profile_task
            raise
        finally:
            if lock_acquired and self._lock is not None:
                with contextlib.suppress(Exception):
                    await self._lock.release_lock(add_lock_key)

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

    async def _require_owned(self, chat_id: int, watch_id: int) -> WatchItem:
        item, is_hidden = await self._repo.get_with_hidden(chat_id, watch_id)
        if item is None or item.chat_id != chat_id:
            raise ValueError("Không tìm thấy kèo trong chat này.")
        item.is_hidden = is_hidden
        return item

    async def set_hidden(self, chat_id: int, watch_id: int, hidden: bool) -> WatchItem:
        item = await self._repo.set_chat_hidden_and_get(chat_id, watch_id, hidden)
        if item is None or item.chat_id != chat_id:
            raise ValueError("Không tìm thấy kèo trong chat này.")
        item.is_hidden = hidden
        return item

    async def save_avatar_file_id(self, item: WatchItem, file_id: str) -> None:
        if not file_id or item.avatar_file_id == file_id:
            return
        item.avatar_file_id = file_id
        await self._repo.update(item)

    async def fetch_avatar_bytes(self, url: str) -> bytes | None:
        return await self._checker.fetch_avatar_bytes(url)

    async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
        return await self._checker.fetch_profile_info(uid)

    async def save_profile_snapshot(
        self,
        item: WatchItem,
        *,
        fb_name: str | None = None,
        avatar_url: str | None = None,
        avatar_file_id: str | None = None,
    ) -> None:
        changed = False
        if fb_name and item.fb_name != fb_name:
            item.fb_name = fb_name
            changed = True
        if avatar_url and item.avatar_url != avatar_url:
            item.avatar_url = avatar_url
            changed = True
        if avatar_file_id is not None and item.avatar_file_id != avatar_file_id:
            item.avatar_file_id = avatar_file_id
            changed = True
        if changed:
            await self._repo.update(item)

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
        item.note, item.price, item.faq = normalize_watch_fields(
            note, price, faq, default_note="Theo dõi nhanh"
        )
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
            item.last_checked_at = now
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
        notify_lock_ttl_seconds: int = 300,
    ) -> None:
        self._repo = repo
        self._checker = checker
        self._lock = lock
        self._notifier = notifier
        self._concurrency = max(1, concurrency)
        self._notify_lock_ttl_seconds = max(30, notify_lock_ttl_seconds)

    async def scan_once(self) -> int:
        items = await self._repo.list_die_items()
        if not items:
            return 0

        # Deduplicate by UID so multiple chats watching the same UID only trigger 1 check
        grouped: dict[str, list[WatchItem]] = {}
        for item in items:
            grouped.setdefault(item.uid, []).append(item)

        sem = asyncio.Semaphore(self._concurrency)
        hidden_by_chat: dict[int, bool] = {}

        async def _get_hidden(chat_id: int) -> bool:
            if chat_id not in hidden_by_chat:
                hidden_by_chat[chat_id] = await self._repo.get_chat_hidden(chat_id)
            return hidden_by_chat[chat_id]

        async def _scan_uid(uid: str, uid_items: list[WatchItem]) -> int:
            try:
                async with sem:
                    res = await self._checker.check(uid)
                if res.outcome != CheckOutcome.LIVE:
                    # Skip redundant Redis writes when item is still DIE or ERROR
                    return 0

                # Layer-2 double verification outside semaphore so verify_delay never blocks DIE scans
                verified = await self._checker.verify_live(uid)
                if verified.outcome != CheckOutcome.LIVE:
                    logger.info(
                        "watcher_double_verify_rejected uid=%s watch_ids=%s second_outcome=%s",
                        uid,
                        [it.id for it in uid_items],
                        verified.outcome.value,
                    )
                    return 0
                fb_name, avatar_url = await self._checker.fetch_profile_info(uid)

                now = datetime.now(UTC)
                count = 0
                for item in uid_items:
                    lock_key = f"notify:{item.id}"
                    locked = await self._lock.acquire_lock(
                        lock_key,
                        ttl_seconds=self._notify_lock_ttl_seconds,
                    )
                    if not locked:
                        continue
                    latest = await self._repo.get(item.id)
                    if latest is None or latest.status != WatchStatus.DIE:
                        await self._lock.release_lock(lock_key)
                        continue
                    latest.last_checked_at = now
                    latest.status = WatchStatus.LIVE
                    latest.live_detected_at = now
                    if fb_name:
                        latest.fb_name = fb_name
                    if avatar_url:
                        latest.avatar_url = avatar_url
                    latest.is_hidden = await _get_hidden(latest.chat_id)
                    try:
                        await self._notifier.notify_live(latest)
                    except Exception as exc:
                        logger.warning(
                            "watcher_notify_failed watch_id=%s chat_id=%s error=%s",
                            latest.id,
                            latest.chat_id,
                            exc,
                        )
                        await self._lock.release_lock(lock_key)
                        continue
                    await self._repo.update(latest, previous_status=WatchStatus.DIE)
                    count += 1
                return count
            except Exception as exc:
                logger.warning("watcher_scan_uid_error uid=%s error=%s", uid, exc)
                return 0

        results = await asyncio.gather(
            *(_scan_uid(uid, uid_items) for uid, uid_items in grouped.items())
        )
        notified = sum(results)
        if notified > 0:
            logger.info(
                "watcher_scan_notified count=%s scanned=%s unique_uids=%s",
                notified,
                len(items),
                len(grouped),
            )
        return notified
