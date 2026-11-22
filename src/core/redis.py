from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from redis.asyncio import Redis


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


class RedisLock:
    """Lightweight Redis distributed lock for deduplicating LIVE notifications."""

    def __init__(self, redis: Redis, prefix: str = "checklivebot") -> None:
        self._redis = redis
        self._prefix = prefix

    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool:
        acquired = await self._redis.set(
            f"{self._prefix}:lock:{key}",
            "1",
            nx=True,
            ex=ttl_seconds,
        )
        return bool(acquired)
