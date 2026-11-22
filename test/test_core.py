from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest

from src.core.checker import CurlCffiFacebookChecker, ProxyPool, parse_graph_picture_payload
from src.core.config import Settings
from src.core.ports import CheckOutcome
from src.core.redis import RedisCache, build_arq_redis_settings


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


def test_settings_prefers_redis_tls_url_and_parses_proxies() -> None:
    s = Settings(
        BOT_TOKEN="123:abc",
        REDIS_URL="redis://localhost:6379/0",
        REDIS_TLS_URL="rediss://:secret@ec2-host.amazonaws.com:12345/0",
        WEBHOOK_BASE_URL="https://demo.herokuapp.com/",
        WEBHOOK_PATH="/webhook",
        PROXY_LIST="http://p1:8080, socks5://p2:1080 ",
    )
    assert s.effective_redis_url == "rediss://:secret@ec2-host.amazonaws.com:12345/0"
    assert s.proxy_urls == ["http://p1:8080", "socks5://p2:1080"]
    assert s.full_webhook_url == "https://demo.herokuapp.com/webhook"

    s_json = Settings(PROXY_LIST='["http://p1:8080", "socks5://p2:1080"]')
    assert s_json.proxy_urls == ["http://p1:8080", "socks5://p2:1080"]

    arq_tls = build_arq_redis_settings(s.effective_redis_url)
    assert arq_tls.ssl is True
    assert arq_tls.ssl_cert_reqs == "none"
    assert arq_tls.host == "ec2-host.amazonaws.com"
    assert arq_tls.port == 12345

    upstash_arq = build_arq_redis_settings("redis://default:pwd@demo-123.upstash.io:6379")
    assert upstash_arq.ssl is True
    assert upstash_arq.ssl_cert_reqs == "none"


def test_parse_graph_picture_payload() -> None:
    live = {"data": {"height": 50, "width": 50, "url": "https://scontent.xx.fbcdn.net/a.jpg"}}
    assert parse_graph_picture_payload(live)[0] == CheckOutcome.LIVE
    assert parse_graph_picture_payload({"error": {"code": 100}})[0] == CheckOutcome.DIE
    assert parse_graph_picture_payload({"data": {}})[0] == CheckOutcome.DIE


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


@pytest.mark.asyncio
async def test_redis_cache_lock_rate_limit_and_bulk_payload() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = RedisCache(redis, prefix="test")

    await cache.set_cached_outcome("10001", CheckOutcome.LIVE, 600)
    await cache.set_cached_outcome("10002", CheckOutcome.DIE, 600)
    await cache.set_cached_outcome("10003", CheckOutcome.ERROR, 600)

    cached = await cache.get_cached_outcomes(["10001", "10002", "10003", "10004"])
    assert cached == {"10001": CheckOutcome.LIVE, "10002": CheckOutcome.DIE}

    assert await cache.acquire_lock("notify:1", 60) is True
    assert await cache.acquire_lock("notify:1", 60) is False

    assert await cache.check_rate_limit(99, 2) is True
    assert await cache.check_rate_limit(99, 2) is True
    assert await cache.check_rate_limit(99, 2) is False

    await cache.save_bulk_payload("job1", ["10001", "10002"])
    assert await cache.load_bulk_payload("job1") == ["10001", "10002"]
    await cache.delete_bulk_payload("job1")
    assert await cache.load_bulk_payload("job1") == []

    await redis.aclose()
