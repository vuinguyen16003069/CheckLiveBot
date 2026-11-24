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
    parse_public_profile_html,
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
        ({"error": {"code": 4}}, 400, CheckOutcome.ERROR, "graph_transient_4"),
        ({"error": {"code": 17}}, 400, CheckOutcome.ERROR, "graph_transient_17"),
        (
            {"error": {"code": 999, "is_transient": True}},
            400,
            CheckOutcome.ERROR,
            "graph_transient_999",
        ),
        ({"data": None}, 200, CheckOutcome.DIE, "no_data_http_200"),
        ({"data": {}}, 200, CheckOutcome.DIE, "missing_avatar_dimensions"),
        (
            {"data": {"height": True, "width": 50, "url": "https://scontent.xx.fbcdn.net/a.jpg"}},
            200,
            CheckOutcome.DIE,
            "missing_avatar_dimensions",
        ),
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
    await lock.release_lock("notify:1")
    assert await lock.acquire_lock("notify:1", 60) is True

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
    assert 55 <= throttle.retry_after_seconds(99) <= 60
    now_ref[0] += 61.0
    assert throttle.check_rate_limit(99) is True
    assert throttle.retry_after_seconds(99) == 0

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


@pytest.mark.asyncio
async def test_proxy_pool_cooldown_and_singleflight_and_transient_retry() -> None:
    # Health-aware ProxyPool skips failing proxy and recovers on success
    pool = ProxyPool(["http://p1:8080", "http://p2:8080"], base_cooldown_seconds=30.0)
    assert pool.next_proxy() == "http://p1:8080"
    pool.report_failure("http://p1:8080")
    assert pool.next_proxy() == "http://p2:8080"
    assert pool.next_proxy() == "http://p2:8080"
    pool.report_success("http://p1:8080")
    assert pool.next_proxy() == "http://p1:8080"

    # Graph transient error (code 4) retries with next proxy instead of false DIE
    transient_session = _FakeSession(
        [
            _resp(400, {"error": {"code": 4, "message": "Rate limit"}}),
            _resp(200, {"data": {"height": 50, "width": 50, "url": "https://img/a.jpg"}}),
        ]
    )
    checker_t = CurlCffiFacebookChecker(
        timeout_seconds=4.0,
        max_retries=2,
        proxies=["http://p1:8080", "http://p2:8080"],
        session=transient_session,  # type: ignore[arg-type]
    )
    res_t = await checker_t.check("100088")
    assert res_t.outcome == CheckOutcome.LIVE
    assert len(transient_session.calls) == 2
    assert transient_session.calls[0][1]["timeout"] == (2.0, 4.0)

    # Singleflight coalesces concurrent check() calls into 1 HTTP request; verify_live stays independent
    class _SlowSession:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, url: str, **kwargs: Any) -> Any:
            self.calls += 1
            await asyncio.sleep(0.05)
            return _resp(200, {"data": {"height": 50, "width": 50, "url": "https://img/a.jpg"}})

        async def close(self) -> None:
            return None

    slow_session = _SlowSession()
    checker_sf = CurlCffiFacebookChecker(
        timeout_seconds=4.0,
        max_retries=1,
        verify_delay_seconds=0.0,
        session=slow_session,  # type: ignore[arg-type]
    )
    results = await asyncio.gather(*(checker_sf.check("100077") for _ in range(5)))
    assert all(r.outcome == CheckOutcome.LIVE for r in results)
    assert slow_session.calls == 1

    verified = await checker_sf.verify_live("100077")
    assert verified.outcome == CheckOutcome.LIVE
    assert slow_session.calls == 2


def test_parse_public_profile_html_extracts_name_and_avatar() -> None:
    html_ok = (
        "<html><head><title>Vui Nguyễn (@vuinguyen.2026) • Facebook</title>"
        '<meta property="og:title" content="Vui Nguyễn" />'
        '<meta property="og:image" content="https://scontent.xx.fbcdn.net/v/avt.jpg" />'
        "</head></html>"
    )
    assert parse_public_profile_html(html_ok) == (
        "Vui Nguyễn",
        "https://scontent.xx.fbcdn.net/v/avt.jpg",
    )

    html_die = (
        "<html><head><title>Facebook</title>"
        '<meta property="og:image" content="https://static.xx.fbcdn.net/rsrc.php/fb_icon.png" />'
        "</head></html>"
    )
    assert parse_public_profile_html(html_die) == (None, None)


@pytest.mark.asyncio
async def test_fetch_profile_info_uses_singleflight_and_ttl_cache() -> None:
    class _ProfileSession:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, url: str, **kwargs: Any) -> Any:
            self.calls += 1
            await asyncio.sleep(0.03)
            return SimpleNamespace(
                text=(
                    '<html><head><meta property="og:title" content="Mark Zuckerberg" />'
                    '<meta property="og:image" content="https://scontent.xx.fbcdn.net/a.jpg" />'
                    "</head></html>"
                )
            )

        async def close(self) -> None:
            return None

    sess = _ProfileSession()
    checker = CurlCffiFacebookChecker(session=sess)  # type: ignore[arg-type]
    # 4 concurrent calls coalesce into 1 HTTP request
    batch = await asyncio.gather(*(checker.fetch_profile_info("4") for _ in range(4)))
    assert all(r == ("Mark Zuckerberg", "https://scontent.xx.fbcdn.net/a.jpg") for r in batch)
    assert sess.calls == 1
    # Subsequent call within 60s hits in-memory cache (0 HTTP requests)
    again = await checker.fetch_profile_info("4")
    assert again == ("Mark Zuckerberg", "https://scontent.xx.fbcdn.net/a.jpg")
    assert sess.calls == 1
    await checker.close()


def test_config_env_file_parsing_overrides_and_formatters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging
    from pathlib import Path

    from pydantic import ValidationError

    from src.core.config import _ConsoleFormatter, _JsonFormatter, _load_env_file  # type: ignore[reportPrivateUsage]

    missing = _load_env_file(Path(tmp_path) / "nonexistent.env")
    assert missing == {}

    env_file = Path(tmp_path) / ".env"
    env_file.write_text(
        "\n# comment line\n"
        "INVALID_LINE_NO_EQUALS\n"
        " =empty_key\n"
        'BOT_TOKEN="quoted-token"\n'
        "REDIS_URL='redis://custom:6379/1'\n"
        "CONCURRENCY=42\n"
        "PROXY_LIST=http://a:80, http://b:80\n",
        encoding="utf-8",
    )
    parsed = _load_env_file(env_file)
    assert parsed["BOT_TOKEN"] == "quoted-token"
    assert parsed["REDIS_URL"] == "redis://custom:6379/1"
    assert parsed["CONCURRENCY"] == "42"

    # Explicit overrides take priority over os.environ and .env (including lowercase field name)
    monkeypatch.setenv("CONCURRENCY", "77")
    s_env = Settings(BOT_TOKEN="t", REDIS_URL="redis://localhost:6379/0")
    assert s_env.concurrency == 77
    s_override = Settings(bot_token="override", concurrency=15)
    assert s_override.bot_token == "override"
    assert s_override.concurrency == 15

    with pytest.raises(ValidationError):
        Settings.model_validate({"BOT_TOKEN": "t", "CONCURRENCY": 9999})
    with pytest.raises(ValidationError):
        Settings.model_validate({"BOT_TOKEN": "t", "POLL_INTERVAL_SECONDS": 1})

    # Formatters with and without exception info
    json_fmt = _JsonFormatter()
    console_fmt = _ConsoleFormatter()
    rec = logging.LogRecord("test.logger", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    assert '"message": "hello world"' in json_fmt.format(rec)
    assert "hello world" in console_fmt.format(rec)

    try:
        raise ValueError("sample failure")
    except ValueError:
        import sys

        exc_rec = logging.LogRecord(
            "test.logger", logging.ERROR, __file__, 1, "boom", (), sys.exc_info()
        )
    assert '"exception"' in json_fmt.format(exc_rec)
    assert "ValueError: sample failure" in console_fmt.format(exc_rec)


@pytest.mark.asyncio
async def test_middleware_pruning_and_anonymous_event(monkeypatch: pytest.MonkeyPatch) -> None:
    now_ref = [500.0]
    monkeypatch.setattr("src.core.middleware.time.monotonic", lambda: now_ref[0])
    mw = ThrottlingMiddleware(limit_per_minute=2)

    # Fill > 2048 stale users to trigger opportunistic pruning
    for uid in range(2050):
        assert mw.check_rate_limit(uid) is True
    now_ref[0] += 120.0
    assert mw.check_rate_limit(99999) is True
    assert mw.retry_after_seconds(99999) == 0

    # Event with from_user=None bypasses throttling
    handler = AsyncMock(return_value="passed")
    anon_msg = AsyncMock(spec=Message)
    anon_msg.from_user = None
    assert await mw(handler, anon_msg, {}) == "passed"
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_checker_stream_head_adaptive_429_redirects_and_profile_edge_cases() -> None:
    # 1. parse_public_profile_html: reversed attr order, <title> fallback, invalid names/images
    html_reversed = (
        '<html><head><meta content="Nguyễn &amp; Bạn" property="og:title" />'
        '<meta content="https://scontent.xx.fbcdn.net/img.jpg" property="og:image" />'
        "</head></html>"
    )
    assert parse_public_profile_html(html_reversed) == (
        "Nguyễn & Bạn",
        "https://scontent.xx.fbcdn.net/img.jpg",
    )

    html_title_fallback = (
        "<html><head><title>Jane Doe (@janedoe) • Facebook</title>"
        '<meta property="og:image" content="not-a-url" /></head></html>'
    )
    assert parse_public_profile_html(html_title_fallback) == ("Jane Doe", None)
    assert parse_public_profile_html("<title>Log in or sign up</title>") == (None, None)
    assert parse_public_profile_html("") == (None, None)

    # 2. Streaming aiter_content stops early at </head> and always calls aclose()
    class _StreamResponse:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self.closed = False
            self.yielded = 0

        async def aiter_content(self) -> Any:
            for c in self._chunks:
                self.yielded += 1
                yield c

        async def aclose(self) -> None:
            self.closed = True

    stream_resp = _StreamResponse(
        [
            b'<html><head><meta property="og:title" content="Stream User" />',
            b'<meta property="og:image" content="https://scontent.xx.fbcdn.net/s.jpg" /></head>',
            b"<body>HUGE BODY SHOULD NOT BE READ</body>",
        ]
    )
    stream_session = _FakeSession([stream_resp])
    checker_stream = CurlCffiFacebookChecker(session=stream_session)  # type: ignore[arg-type]
    assert await checker_stream.fetch_profile_info("10001") == (
        "Stream User",
        "https://scontent.xx.fbcdn.net/s.jpg",
    )
    assert stream_resp.yielded == 2
    assert stream_resp.closed is True

    # 3. Profile fetch error returns (None, None) without raising
    err_session = _FakeSession([RuntimeError("profile network down")])
    checker_err = CurlCffiFacebookChecker(
        proxies=["http://p1:8080"],
        session=err_session,  # type: ignore[arg-type]
    )
    assert await checker_err.fetch_profile_info("10002") == (None, None)

    # 4. HTTP 302 redirect is treated as retryable; 429 triggers adaptive rate-limit backoff
    redirect_and_429 = _FakeSession(
        [
            _resp(302, ValueError("redirect html")),
            _resp(429, {}),
            _resp(429, {}),
            _resp(429, {}),
            _resp(200, {"data": {"height": 50, "width": 50, "url": "https://scontent/ok.jpg"}}),
        ]
    )
    checker_rl = CurlCffiFacebookChecker(
        max_retries=5,
        session=redirect_and_429,  # type: ignore[arg-type]
    )
    res_rl = await checker_rl.check("10003")
    assert res_rl.outcome == CheckOutcome.LIVE
    assert len(redirect_and_429.calls) == 5

    # 5. ProxyPool all-cooldown fallback picks earliest-ready proxy and handles None reports
    pool = ProxyPool(["http://p1:8080", "http://p2:8080"], base_cooldown_seconds=30.0)
    assert pool.has_healthy_proxy() is True
    pool.report_failure(None)
    pool.report_success(None)
    pool.report_failure("http://p1:8080")
    pool.report_failure("http://p2:8080")
    assert pool.has_healthy_proxy() is False
    assert pool.next_proxy() == "http://p1:8080"
    assert ProxyPool([]).has_healthy_proxy() is False


@pytest.mark.asyncio
async def test_runtime_run_bot_validates_token_and_closes_redis_on_ping_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.runtime import install_uvloop_if_available, run_bot

    install_uvloop_if_available()

    with pytest.raises(RuntimeError, match="BOT_TOKEN is required"):
        await run_bot(Settings(BOT_TOKEN=""))

    fake_redis = AsyncMock()
    fake_redis.ping = AsyncMock(side_effect=ConnectionError("redis down"))
    fake_redis.aclose = AsyncMock()

    def _fake_client(_url: str) -> AsyncMock:
        return fake_redis

    monkeypatch.setattr("src.core.runtime.create_redis_client", _fake_client)

    with pytest.raises(ConnectionError, match="redis down"):
        await run_bot(Settings(BOT_TOKEN="123:abc", REDIS_URL="redis://localhost:6379/0"))
    fake_redis.aclose.assert_awaited_once()
