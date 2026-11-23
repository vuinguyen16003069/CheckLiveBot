from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from aiogram.types import CallbackQuery, Message

from src.core.checker import (
    CurlCffiFacebookChecker,
    ProxyPool,
    parse_graph_picture_payload,
)
from src.core.config import Settings, setup_logging
from src.core.middleware import ThrottlingMiddleware
from src.core.ports import CheckOutcome
from src.core.redis import RedisLock, create_redis_client, normalize_redis_url
from src.core.runtime import run_watcher_loop


class _FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def close(self) -> None:
        self.closed = True


def _resp(status_code: int, payload: Any) -> Any:
    if isinstance(payload, Exception):

        def _bad_json() -> Any:
            raise payload

        return SimpleNamespace(status_code=status_code, json=_bad_json)
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


def test_settings_logging_and_redis_client_config() -> None:
    s = Settings(
        BOT_TOKEN="123:abc",
        REDIS_URL="redis://localhost:6379/0",
        PROXY_LIST="http://p1:8080, socks5://p2:1080 , ",
    )
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.proxy_urls == ["http://p1:8080", "socks5://p2:1080"]
    assert Settings(BOT_TOKEN="1:x", PROXY_LIST="  ,  ").proxy_urls == []

    assert (
        normalize_redis_url("  redis://default:pwd@demo-123.upstash.io:6379  ")
        == "rediss://default:pwd@demo-123.upstash.io:6379"
    )
    assert (
        normalize_redis_url("rediss://default:pwd@demo-123.upstash.io:6379")
        == "rediss://default:pwd@demo-123.upstash.io:6379"
    )
    assert normalize_redis_url("redis://localhost:6379/0") == "redis://localhost:6379/0"

    plain_client = create_redis_client("redis://localhost:6379/0")
    tls_client = create_redis_client("redis://default:pwd@demo-123.upstash.io:6379")
    assert plain_client.connection_pool.connection_kwargs["socket_connect_timeout"] == 5
    assert plain_client.connection_pool.connection_kwargs["socket_timeout"] == 10
    assert tls_client.connection_pool.connection_kwargs["ssl_cert_reqs"] is None

    setup_logging("INFO", "console")
    setup_logging("WARNING", "json")


@pytest.mark.parametrize(
    ("payload", "status_code", "expected_outcome", "expected_detail"),
    [
        (
            {"data": {"height": 50, "width": 50, "url": "https://scontent.xx.fbcdn.net/a.jpg"}},
            200,
            CheckOutcome.LIVE,
            None,
        ),
        (
            {
                "data": {
                    "height": 50,
                    "width": 50,
                    "is_silhouette": True,
                    "url": "https://scontent.xx.fbcdn.net/real_avatar.jpg",
                }
            },
            200,
            CheckOutcome.LIVE,
            None,
        ),
        ({"error": {"code": 100}}, 200, CheckOutcome.DIE, "graph_error"),
        ({"data": None}, 200, CheckOutcome.DIE, "no_data_http_200"),
        ({"data": {}}, 200, CheckOutcome.DIE, "missing_avatar_dimensions"),
        (
            {"data": {"height": 0, "width": 50, "url": "https://scontent.xx.fbcdn.net/a.jpg"}},
            200,
            CheckOutcome.DIE,
            "missing_avatar_dimensions",
        ),
        (
            {"data": {"height": 50, "width": 50, "url": "   "}},
            200,
            CheckOutcome.DIE,
            "missing_avatar_dimensions",
        ),
        (
            {
                "data": {
                    "height": 50,
                    "width": 50,
                    "is_silhouette": True,
                    "url": "https://static.xx.fbcdn.net/rsrc.php/v3/yo/r/UlIqmHJn-SK.gif",
                }
            },
            200,
            CheckOutcome.DIE,
            "silhouette_placeholder",
        ),
        (
            {
                "data": {
                    "height": 50,
                    "width": 50,
                    "is_silhouette": True,
                    "url": "https://scontent.xx.fbcdn.net/v/t1.30497-1/C5yt7C_3z_G.jpg",
                }
            },
            200,
            CheckOutcome.DIE,
            "silhouette_placeholder",
        ),
        (["unexpected"], 200, CheckOutcome.ERROR, "unexpected_payload_http_200"),
        (None, 502, CheckOutcome.ERROR, "unexpected_payload_http_502"),
    ],
)
def test_parse_graph_picture_payload_matrix(
    payload: Any,
    status_code: int,
    expected_outcome: CheckOutcome,
    expected_detail: str | None,
) -> None:
    assert parse_graph_picture_payload(payload, status_code) == (expected_outcome, expected_detail)


async def test_checker_live_die_404_and_retry_with_proxy_rotation() -> None:
    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    assert len(pool) == 2
    assert pool.next_proxy() == "http://p1:8080"
    assert pool.next_proxy() == "http://p2:8080"
    assert pool.next_proxy() == "http://p1:8080"
    assert ProxyPool([]).next_proxy() is None

    session = _FakeSession(
        [
            _resp(429, {"error": "rate limit"}),
            _resp(503, {"error": "upstream"}),
            _resp(200, {"data": {"height": 50, "width": 50, "url": "https://img/a.jpg"}}),
        ]
    )
    async with CurlCffiFacebookChecker(
        timeout_seconds=5,
        max_retries=3,
        proxies=["http://p1:8080", "http://p2:8080"],
        session=session,  # type: ignore[arg-type]
    ) as checker:
        res = await checker.check("100012345678901")
    assert res.outcome == CheckOutcome.LIVE
    assert len(session.calls) == 3
    assert session.calls[0][1]["proxy"] == "http://p1:8080"
    assert session.calls[1][1]["proxy"] == "http://p2:8080"
    assert session.calls[2][1]["proxy"] == "http://p1:8080"
    # Injected session must NOT be closed by context manager
    assert session.closed is False

    # HTTP 404 with non-JSON body returns DIE immediately without retrying
    session_404 = _FakeSession([_resp(404, ValueError("not json"))])
    checker_404 = CurlCffiFacebookChecker(
        timeout_seconds=2,
        max_retries=3,
        session=session_404,  # type: ignore[arg-type]
    )
    res_404 = await checker_404.check("1000999")
    assert res_404.outcome == CheckOutcome.DIE
    assert res_404.detail == "http_404"
    assert len(session_404.calls) == 1


async def test_checker_returns_error_on_exhausted_retries_and_invalid_json() -> None:
    session = _FakeSession([TimeoutError("boom"), _resp(200, ValueError("broken json"))])
    checker = CurlCffiFacebookChecker(
        timeout_seconds=1,
        max_retries=2,
        session=session,  # type: ignore[arg-type]
    )
    res = await checker.check("100012345678901")
    assert res.outcome == CheckOutcome.ERROR
    assert res.detail is not None
    assert "invalid_json_http_200" in res.detail


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
async def test_redis_lock_and_throttling_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    lock = RedisLock(redis, prefix="test")

    assert await lock.acquire_lock("notify:1", 60) is True
    assert await lock.acquire_lock("notify:1", 60) is False

    # Unlimited mode (limit=0)
    unlimited = ThrottlingMiddleware(limit_per_minute=0)
    assert all(unlimited.check_rate_limit(1) for _ in range(50))

    # Sliding window expiry
    now_ref = [1000.0]
    monkeypatch.setattr("src.core.middleware.time.monotonic", lambda: now_ref[0])
    throttle = ThrottlingMiddleware(limit_per_minute=2)
    assert throttle.check_rate_limit(99) is True
    assert throttle.check_rate_limit(99) is True
    assert throttle.check_rate_limit(99) is False
    now_ref[0] += 61.0
    assert throttle.check_rate_limit(99) is True

    # Middleware __call__ blocks Message & CallbackQuery when rate-limited
    handler = AsyncMock(return_value="ok")
    real_msg = AsyncMock(spec=Message)
    real_msg.from_user = SimpleNamespace(id=501)
    real_msg.answer = AsyncMock()
    real_cb = AsyncMock(spec=CallbackQuery)
    real_cb.from_user = SimpleNamespace(id=501)
    real_cb.answer = AsyncMock()

    mw = ThrottlingMiddleware(limit_per_minute=1)
    assert await mw(handler, real_msg, {}) == "ok"
    assert handler.await_count == 1
    assert await mw(handler, real_msg, {}) is None
    assert real_msg.answer.await_count == 1
    assert await mw(handler, real_cb, {}) is None
    assert real_cb.answer.await_count == 1

    await redis.aclose()


@pytest.mark.asyncio
async def test_run_watcher_loop_recovers_from_errors_and_stops() -> None:
    stop_event = asyncio.Event()
    calls = 0

    class _FlakyScanner:
        async def scan_once(self) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient network error")
            stop_event.set()
            return 1

    # interval_seconds=3 is minimum; override wait_for timeout via immediate stop on 2nd call
    task = asyncio.create_task(run_watcher_loop(_FlakyScanner(), 3, stop_event))  # type: ignore[arg-type]
    await asyncio.wait_for(task, timeout=6.0)
    assert calls == 2
