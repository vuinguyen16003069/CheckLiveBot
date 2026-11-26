from __future__ import annotations

import contextlib
import uuid
from typing import Any
from urllib.parse import urlparse

from redis.asyncio import Redis
from redis.exceptions import WatchError


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
        "socket_connect_timeout": 5,
        "socket_timeout": 10,
        "health_check_interval": 30,
    }
    if normalized.lower().startswith("rediss://"):
        kwargs["ssl_cert_reqs"] = None
    return Redis.from_url(normalized, **kwargs)


class RedisLock:
    """Lightweight token-safe Redis distributed lock."""

    def __init__(self, redis: Redis, prefix: str = "checklivebot") -> None:
        self._redis = redis
        self._prefix = prefix
        self._tokens: dict[str, str] = {}

    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool:
        token = uuid.uuid4().hex
        acquired = await self._redis.set(
            f"{self._prefix}:lock:{key}",
            token,
            nx=True,
            ex=ttl_seconds,
        )
        if acquired:
            self._tokens[key] = token
            return True
        return False

    async def release_lock(self, key: str) -> None:
        token = self._tokens.pop(key, None)
        if token is None:
            return
        full_key = f"{self._prefix}:lock:{key}"
        async with self._redis.pipeline(transaction=True) as pipe:
            with contextlib.suppress(WatchError):
                await pipe.watch(full_key)
                if await pipe.get(full_key) == token:
                    pipe.multi()
                    pipe.delete(full_key)
                    await pipe.execute()
                else:
                    await pipe.unwatch()
