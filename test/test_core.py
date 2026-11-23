from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest

from src.core.checker import (
    CurlCffiFacebookChecker,
    ProxyPool,
    parse_graph_picture_payload,
)
from src.core.config import Settings
from src.core.middleware import ThrottlingMiddleware
from src.core.ports import CheckOutcome
from src.core.redis import RedisLock, normalize_redis_url


class _FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def close(self) -> None:
        return None


def _resp(status_code: int, payload: Any) -> Any:
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


def test_settings_and_redis_url_normalization() -> None:
    s = Settings(
        BOT_TOKEN="123:abc",
        REDIS_URL="redis://localhost:6379/0",
        PROXY_LIST="http://p1:8080, socks5://p2:1080 ",
    )
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.proxy_urls == ["http://p1:8080", "socks5://p2:1080"]
    assert (
        normalize_redis_url("redis://default:pwd@demo-123.upstash.io:6379")
        == "rediss://default:pwd@demo-123.upstash.io:6379"
    )


def test_parse_graph_picture_payload() -> None:
    live = {"data": {"height": 50, "width": 50, "url": "https://scontent.xx.fbcdn.net/a.jpg"}}
    assert parse_graph_picture_payload(live)[0] == CheckOutcome.LIVE
    assert parse_graph_picture_payload({"error": {"code": 100}})[0] == CheckOutcome.DIE
    assert parse_graph_picture_payload({"data": {}})[0] == CheckOutcome.DIE

    # Silhouette placeholder from static.xx.fbcdn.net must be classified as DIE
    placeholder = {
        "data": {
            "height": 50,
            "width": 50,
            "is_silhouette": True,
            "url": "https://static.xx.fbcdn.net/rsrc.php/v3/yo/r/UlIqmHJn-SK.gif",
        }
    }
    outcome, detail = parse_graph_picture_payload(placeholder)
    assert outcome == CheckOutcome.DIE
    assert detail == "silhouette_placeholder"

    # Mixed-case silhouette hash marker must also match case-insensitively
    hash_placeholder = {
        "data": {
            "height": 50,
            "width": 50,
            "is_silhouette": True,
            "url": "https://scontent.xx.fbcdn.net/v/t1.30497-1/C5yt7C_3z_G.jpg",
        }
    }
    assert parse_graph_picture_payload(hash_placeholder) == (
        CheckOutcome.DIE,
        "silhouette_placeholder",
    )


async def test_checker_live_die_and_retry_with_proxy_rotation() -> None:
    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    assert pool.next_proxy() == "http://p1:8080"
    assert pool.next_proxy() == "http://p2:8080"
    assert pool.next_proxy() == "http://p1:8080"

    session = _FakeSession(
        [
            _resp(429, {"error": "rate limit"}),
            _resp(200, {"data": {"height": 50, "width": 50, "url": "https://img/a.jpg"}}),
        ]
    )
    checker = CurlCffiFacebookChecker(
        timeout_seconds=5,
        max_retries=3,
        proxies=["http://p1:8080", "http://p2:8080"],
        session=session,  # type: ignore[arg-type]
    )
    res = await checker.check("100012345678901")
    assert res.outcome == CheckOutcome.LIVE
    assert len(session.calls) == 2
    assert session.calls[0][1]["proxy"] == "http://p1:8080"
    assert session.calls[1][1]["proxy"] == "http://p2:8080"


async def test_checker_returns_error_on_exhausted_retries() -> None:
    session = _FakeSession([TimeoutError("boom"), TimeoutError("boom")])
    checker = CurlCffiFacebookChecker(
        timeout_seconds=1,
        max_retries=2,
        session=session,  # type: ignore[arg-type]
    )
    res = await checker.check("100012345678901")
    assert res.outcome == CheckOutcome.ERROR


async def test_checker_verify_live_graph_only() -> None:
    graph_live = _resp(200, {"data": {"height": 50, "width": 50, "url": "https://img/a.jpg"}})
    session = _FakeSession([graph_live])
    checker = CurlCffiFacebookChecker(
        timeout_seconds=2,
        max_retries=1,
        verify_delay_seconds=0,
        max_clients=120,
        session=session,  # type: ignore[arg-type]
    )
    res = await checker.verify_live("100012345678901")
    assert res.outcome == CheckOutcome.LIVE
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_redis_lock_and_in_memory_rate_limit() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    lock = RedisLock(redis, prefix="test")

    assert await lock.acquire_lock("notify:1", 60) is True
    assert await lock.acquire_lock("notify:1", 60) is False

    throttle = ThrottlingMiddleware(limit_per_minute=2)
    assert throttle.check_rate_limit(99) is True
    assert throttle.check_rate_limit(99) is True
    assert throttle.check_rate_limit(99) is False

    await redis.aclose()
