from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from arq.connections import ArqRedis, RedisSettings
from redis.asyncio import Redis

from src.core.ports import CheckOutcome


def normalize_redis_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if (
        parsed.scheme.lower() == "redis"
        and parsed.hostname
        and parsed.hostname.lower().endswith(".upstash.io")
    ):
        return "rediss://" + cleaned[len("redis://") :]
    return cleaned


def create_redis_client(url: str) -> Redis:
    normalized = normalize_redis_url(url)
    kwargs: dict[str, Any] = {
        "decode_responses": True,
        "socket_keepalive": True,
        "health_check_interval": 30,
    }
    if normalized.lower().startswith("rediss://"):
        kwargs["ssl_cert_reqs"] = None
    return Redis.from_url(normalized, **kwargs)


def build_arq_redis_settings(url: str) -> RedisSettings:
    parsed = urlparse(normalize_redis_url(url))
    is_tls = parsed.scheme.lower() == "rediss"
    database = int(parsed.path.lstrip("/") or 0)
    if is_tls:
        return RedisSettings(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            username=parsed.username,
            password=parsed.password,
            database=database,
            ssl=True,
            ssl_cert_reqs="none",
        )
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        username=parsed.username,
        password=parsed.password,
        database=database,
        ssl=False,
    )


class RedisCache:
    def __init__(self, redis: Redis, prefix: str = "checklivebot") -> None:
        self._redis = redis
        self._prefix = prefix

    def _k(self, suffix: str) -> str:
        return f"{self._prefix}:{suffix}"

    async def get_cached_outcomes(self, uids: Sequence[str]) -> dict[str, CheckOutcome]:
        if not uids:
            return {}
        found: dict[str, CheckOutcome] = {}
        chunk_size = 1000
        for idx in range(0, len(uids), chunk_size):
            batch = uids[idx : idx + chunk_size]
            keys = [self._k(f"check_cache:{uid}") for uid in batch]
            values = await self._redis.mget(keys)
            for uid, raw in zip(batch, values, strict=True):
                if raw in (CheckOutcome.LIVE.value, CheckOutcome.DIE.value):
                    found[uid] = CheckOutcome(raw)
        return found

    async def set_cached_outcome(
        self,
        uid: str,
        outcome: CheckOutcome,
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0 or outcome == CheckOutcome.ERROR:
            return
        await self._redis.set(self._k(f"check_cache:{uid}"), outcome.value, ex=ttl_seconds)

    async def set_cached_outcomes(
        self,
        outcomes: dict[str, CheckOutcome],
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0 or not outcomes:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for uid, outcome in outcomes.items():
                if outcome != CheckOutcome.ERROR:
                    pipe.set(self._k(f"check_cache:{uid}"), outcome.value, ex=ttl_seconds)
            await pipe.execute()

    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool:
        acquired = await self._redis.set(self._k(f"lock:{key}"), "1", nx=True, ex=ttl_seconds)
        return bool(acquired)

    async def check_rate_limit(self, user_id: int, limit_per_minute: int) -> bool:
        if limit_per_minute <= 0:
            return True
        key = self._k(f"ratelimit:{user_id}")
        count = int(await self._redis.incr(key))
        if count == 1:
            await self._redis.expire(key, 60)
        return count <= limit_per_minute

    async def save_bulk_payload(
        self,
        job_id: str,
        uids: Sequence[str],
        ttl_seconds: int = 3600,
    ) -> None:
        await self._redis.set(
            self._k(f"bulk:{job_id}"),
            json.dumps(list(uids)),
            ex=ttl_seconds,
        )

    async def load_bulk_payload(self, job_id: str) -> list[str]:
        raw = await self._redis.get(self._k(f"bulk:{job_id}"))
        if not raw:
            return []
        data = json.loads(raw)
        return [str(x) for x in data]

    async def delete_bulk_payload(self, job_id: str) -> None:
        await self._redis.delete(self._k(f"bulk:{job_id}"))


class ArqJobQueue:
    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue_bulk_check(
        self,
        job_id: str,
        chat_id: int,
        status_message_id: int,
    ) -> None:
        await self._pool.enqueue_job(
            "process_bulk_check_job",
            job_id,
            chat_id,
            status_message_id,
            _job_id=f"bulk:{job_id}",
        )
