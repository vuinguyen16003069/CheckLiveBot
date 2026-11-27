from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import random
import re
import time
from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlparse

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession

from src.core.ports import CheckOutcome, UidCheckResult

logger = logging.getLogger(__name__)


def _redact_proxy_auth(value: object) -> str:
    return re.sub(r"://[^/\s:@]+:[^/\s@]+@", "://***:***@", str(value))


def _is_allowed_fb_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(
        host == base or host.endswith(f".{base}") for base in ("facebook.com", "fb.com", "fb.watch")
    )


def _meta_content(body: str, prop: str) -> str | None:
    escaped = re.escape(prop)
    for pattern in (
        rf'<meta[^>]+property=["\']{escaped}["\'][^>]+content=(?P<q>["\'])(?P<val>[^<>]*?)(?P=q)',
        rf'<meta[^>]+content=(?P<q>["\'])(?P<val>[^<>]*?)(?P=q)[^>]+property=["\']{escaped}["\']',
    ):
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            return html.unescape(match.group("val")).strip()
    return None


class _RetryableCheckError(Exception):
    pass


class ProxyPool:
    def __init__(
        self,
        proxies: Sequence[str] | None = None,
        *,
        base_cooldown_seconds: float = 15.0,
        max_cooldown_seconds: float = 60.0,
    ) -> None:
        self._proxies = [p.strip() for p in proxies or [] if p.strip()]
        self._base_cooldown = max(1.0, base_cooldown_seconds)
        self._max_cooldown = max(self._base_cooldown, max_cooldown_seconds)
        self._cursor = 0
        self._cooldown_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    def next_proxy(self) -> str | None:
        total = len(self._proxies)
        if total == 0:
            return None

        now = time.monotonic()
        order = [(self._cursor + offset) % total for offset in range(total)]

        def ready_at(idx: int) -> float:
            return self._cooldown_until.get(self._proxies[idx], 0.0)

        chosen = next((idx for idx in order if ready_at(idx) <= now), None)
        if chosen is None:
            chosen = min(order, key=ready_at)

        self._cursor = (chosen + 1) % total
        return self._proxies[chosen]

    def report_failure(self, proxy: str | None) -> None:
        if not proxy:
            return
        failures = self._failures.get(proxy, 0) + 1
        self._failures[proxy] = failures
        cooldown = min(self._base_cooldown * 2 ** (failures - 1), self._max_cooldown)
        self._cooldown_until[proxy] = time.monotonic() + cooldown

    def report_success(self, proxy: str | None) -> None:
        if not proxy:
            return
        self._failures.pop(proxy, None)
        self._cooldown_until.pop(proxy, None)

    def has_healthy_proxy(self) -> bool:
        if not self._proxies:
            return False
        now = time.monotonic()
        return any(self._cooldown_until.get(p, 0.0) <= now for p in self._proxies)

    def __len__(self) -> int:
        return len(self._proxies)


class CurlCffiFacebookChecker:
    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        max_retries: int = 2,
        proxies: Sequence[str] | None = None,
        session: AsyncSession[Any] | None = None,
        verify_delay_seconds: float = 0.5,
        max_clients: int = 100,
    ) -> None:
        self._timeout = timeout_seconds
        self._connect_timeout = min(2.0, timeout_seconds * 0.5)
        self._max_retries = max(1, max_retries)
        self._verify_delay = max(0.0, verify_delay_seconds)
        self._proxy_pool = ProxyPool(proxies)
        self._owns_session = session is None
        self._session: AsyncSession[Any] = session or AsyncSession(
            impersonate="chrome",
            max_clients=max(20, max_clients + 25),
            allow_redirects=False,
            discard_cookies=True,
            curl_options={
                CurlOpt.IPRESOLVE: 1,
                CurlOpt.DNS_CACHE_TIMEOUT: 300,
            },
        )
        self._profile_session: AsyncSession[Any] = session or AsyncSession(
            max_clients=max(10, min(max_clients, 50)),
            allow_redirects=True,
            discard_cookies=True,
            default_headers=False,
            curl_options={
                CurlOpt.IPRESOLVE: 1,
                CurlOpt.DNS_CACHE_TIMEOUT: 300,
            },
        )
        self._http_sem = asyncio.Semaphore(max(1, max_clients))
        self._inflight: dict[str, asyncio.Task[UidCheckResult]] = {}
        self._profile_inflight: dict[str, asyncio.Task[tuple[str | None, str | None]]] = {}
        self._rate_limit_until = 0.0
        self._consecutive_rate_limits = 0

    async def __aenter__(self) -> CurlCffiFacebookChecker:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def check(self, uid: str) -> UidCheckResult:
        task = self._inflight.get(uid)
        if task is None:
            task = asyncio.create_task(self._check_direct(uid))
            self._inflight[uid] = task
            task.add_done_callback(lambda done: self._release(uid, done))
        return await asyncio.shield(task)

    async def verify_live(self, uid: str) -> UidCheckResult:
        await asyncio.sleep(self._verify_delay)
        return await self._check_direct(uid)

    async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
        task = self._profile_inflight.get(uid)
        if task is None:
            task = asyncio.create_task(self._fetch_profile_direct(uid))
            self._profile_inflight[uid] = task
            task.add_done_callback(lambda done: self._release_profile(uid, done))
        return await asyncio.shield(task)

    async def _fetch_profile_direct(self, uid: str) -> tuple[str | None, str | None]:
        url = f"https://www.facebook.com/profile.php?id={uid}"
        kwargs: dict[str, Any] = {
            "timeout": (4.0, max(6.0, self._timeout)),
            "allow_redirects": True,
            "stream": True,
            "headers": {
                "User-Agent": "WhatsApp/2.23.20.0",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        }
        proxy = self._proxy_pool.next_proxy()
        if proxy:
            kwargs["proxy"] = proxy

        response: Any = None
        try:
            async with self._http_sem:
                response = await self._profile_session.get(url, **kwargs)
                status = int(getattr(response, "status_code", 200))
                final_url = str(getattr(response, "url", url) or url)
                if status >= 400:
                    self._proxy_pool.report_failure(proxy)
                    logger.warning(
                        "fb_profile_info_http_error uid=%s status=%s url=%s", uid, status, url
                    )
                    return None, None
                if _is_login_or_checkpoint_url(final_url):
                    self._proxy_pool.report_failure(proxy)
                    logger.warning("fb_profile_info_login_wall uid=%s final_url=%s", uid, final_url)
                    return None, None

                body = await self._read_head(response)
            result = parse_public_profile_html(body) if body else (None, None)
            if result[0] is not None or result[1] is not None:
                self._proxy_pool.report_success(proxy)
                return result
            logger.warning(
                "fb_profile_info_empty uid=%s status=%s url=%s final_url=%s",
                uid,
                status,
                url,
                final_url,
            )
            return None, None
        except Exception as exc:
            self._proxy_pool.report_failure(proxy)
            logger.warning("fb_profile_info_failed uid=%s error=%s", uid, _redact_proxy_auth(exc))
            return None, None
        finally:
            if response is not None and callable(getattr(response, "aclose", None)):
                with contextlib.suppress(Exception):
                    await response.aclose()

    async def fetch_avatar_bytes(self, url: str) -> bytes | None:
        if not url or not url.startswith(("http://", "https://")):
            return None

        kwargs: dict[str, Any] = {
            "timeout": (4.0, max(6.0, self._timeout)),
            "allow_redirects": True,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        }
        proxy = self._proxy_pool.next_proxy()
        if proxy:
            kwargs["proxy"] = proxy

        try:
            async with self._http_sem:
                response: Any = await self._profile_session.get(url, **kwargs)
            status = int(getattr(response, "status_code", 200))
            if status != 200:
                self._proxy_pool.report_failure(proxy)
                logger.warning("fb_avatar_http_error status=%s", status)
                return None
            resp_headers: Any = getattr(response, "headers", None)
            content_type = (
                str(
                    resp_headers.get("content-type", "") or resp_headers.get("Content-Type", "")
                ).lower()
                if callable(getattr(resp_headers, "get", None))
                else ""
            )
            if content_type and not content_type.startswith("image/"):
                self._proxy_pool.report_failure(proxy)
                logger.warning("fb_avatar_non_image content_type=%s", content_type)
                return None
            content = getattr(response, "content", b"")
            if not isinstance(content, (bytes, bytearray)) or not (
                0 < len(content) <= 5 * 1024 * 1024
            ):
                return None
            head_sample = bytes(content[:32]).lstrip().lower()
            if head_sample.startswith((b"<html", b"<!doctype")):
                self._proxy_pool.report_failure(proxy)
                logger.warning("fb_avatar_html_payload")
                return None
            self._proxy_pool.report_success(proxy)
            return bytes(content)
        except Exception as exc:
            self._proxy_pool.report_failure(proxy)
            logger.warning("fb_avatar_download_failed error=%s", _redact_proxy_auth(exc))
            return None

    async def resolve_uid_from_url(self, url: str) -> str | None:
        if not _is_allowed_fb_url(url):
            return None
        kwargs: dict[str, Any] = {
            "timeout": (4.0, max(6.0, self._timeout)),
            "allow_redirects": True,
            "max_redirects": 5,
            "stream": True,
            "headers": {
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            },
        }
        proxy = self._proxy_pool.next_proxy()
        if proxy:
            kwargs["proxy"] = proxy

        response: Any = None
        try:
            async with self._http_sem:
                response = await self._session.get(url, **kwargs)
                final_url = str(getattr(response, "url", "") or "")
                if final_url and (
                    match := re.search(
                        r"(?:profile\.php\?[^#\s]*\bid=|facebook\.com/|fb\.com/)([1-9]\d{0,19})(?:[/?#&]|$)",
                        final_url,
                        re.IGNORECASE,
                    )
                ):
                    self._proxy_pool.report_success(proxy)
                    return match.group(1)

                status = int(getattr(response, "status_code", 200))
                if status >= 400:
                    self._proxy_pool.report_failure(proxy)
                    return None

                uid = await self._read_uid_from_response(response)
            if uid:
                self._proxy_pool.report_success(proxy)
                return uid
            return None
        except Exception as exc:
            self._proxy_pool.report_failure(proxy)
            logger.warning("fb_resolve_uid_failed url=%s error=%s", url, _redact_proxy_auth(exc))
            return None
        finally:
            if response is not None and callable(getattr(response, "aclose", None)):
                with contextlib.suppress(Exception):
                    await response.aclose()

    async def close(self) -> None:
        pending: list[asyncio.Task[Any]] = [
            *self._inflight.values(),
            *self._profile_inflight.values(),
        ]
        self._inflight.clear()
        self._profile_inflight.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._owns_session:
            await self._session.close()
            if self._profile_session is not self._session:
                await self._profile_session.close()

    def _release(self, uid: str, task: asyncio.Task[UidCheckResult]) -> None:
        if self._inflight.get(uid) is task:
            del self._inflight[uid]

    def _release_profile(self, uid: str, task: asyncio.Task[tuple[str | None, str | None]]) -> None:
        if self._profile_inflight.get(uid) is task:
            del self._profile_inflight[uid]

    @staticmethod
    async def _read_head(response: Any) -> str:
        if not callable(getattr(response, "aiter_content", None)):
            text = getattr(response, "text", "")
            return text if isinstance(text, str) else ""

        buf = bytearray()
        async for chunk in response.aiter_content():
            buf.extend(chunk)
            if b"</head>" in buf or b"</HEAD>" in buf or len(buf) >= 262144:
                break
            og_img_idx = max(buf.rfind(b'og:image"'), buf.rfind(b"og:image'"))
            if (
                og_img_idx != -1
                and (b'og:title"' in buf or b"og:title'" in buf)
                and b">" in buf[og_img_idx:]
            ):
                break
        return buf.decode("utf-8", errors="ignore")

    @staticmethod
    async def _read_uid_from_response(response: Any, max_bytes: int = 524288) -> str | None:
        if not callable(getattr(response, "aiter_content", None)):
            text = getattr(response, "text", "")
            return parse_uid_from_profile_html(text) if isinstance(text, str) else None

        buf = bytearray()
        async for chunk in response.aiter_content():
            buf.extend(chunk)
            if uid := parse_uid_from_profile_html(buf.decode("utf-8", errors="ignore")):
                return uid
            if len(buf) >= max_bytes:
                break
        return None

    async def _check_direct(self, uid: str) -> UidCheckResult:
        url = f"https://graph.facebook.com/{uid}/picture?type=normal&redirect=false"
        last_error: _RetryableCheckError | None = None

        for attempt in range(self._max_retries):
            try:
                outcome, detail = await self._fetch_graph_once(url)
            except _RetryableCheckError as exc:
                last_error = exc
                if attempt + 1 < self._max_retries:
                    backoff = min(0.2 * 2**attempt, 1.0)
                    await asyncio.sleep(backoff * random.uniform(0.5, 1.0))  # noqa: S311
            except Exception as exc:
                logger.warning("fb_check_unexpected_error uid=%s error=%s", uid, exc)
                return UidCheckResult(uid=uid, outcome=CheckOutcome.ERROR, detail=str(exc))
            else:
                return UidCheckResult(uid=uid, outcome=outcome, detail=detail)

        logger.warning("fb_check_retry_exhausted uid=%s error=%s", uid, last_error)
        return UidCheckResult(uid=uid, outcome=CheckOutcome.ERROR, detail=str(last_error))

    def _note_rate_limit(self) -> None:
        if self._proxy_pool.has_healthy_proxy():
            return
        self._consecutive_rate_limits += 1
        if self._consecutive_rate_limits >= 3:
            pause = min(0.5 * (2 ** (self._consecutive_rate_limits - 3)), 2.0)
            jitter = pause * random.uniform(0.6, 1.4)  # noqa: S311
            self._rate_limit_until = max(self._rate_limit_until, time.monotonic() + jitter)

    async def _fetch_graph_once(self, url: str) -> tuple[CheckOutcome, str | None]:
        wait = self._rate_limit_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

        kwargs: dict[str, Any] = {"timeout": (self._connect_timeout, self._timeout)}
        proxy = self._proxy_pool.next_proxy()
        if proxy:
            kwargs["proxy"] = proxy

        try:
            async with self._http_sem:
                response: Any = await self._session.get(url, **kwargs)
        except Exception as exc:
            self._proxy_pool.report_failure(proxy)
            raise _RetryableCheckError(f"request_failed: {_redact_proxy_auth(exc)}") from exc

        status = int(response.status_code)
        if status in (403, 429) or status >= 500 or 300 <= status < 400:
            self._proxy_pool.report_failure(proxy)
            if status == 429:
                self._note_rate_limit()
            raise _RetryableCheckError(f"http_{status}")

        try:
            payload: Any = response.json()
        except Exception as exc:
            if status == 404:
                self._proxy_pool.report_success(proxy)
                self._consecutive_rate_limits = 0
                return CheckOutcome.DIE, "http_404"
            self._proxy_pool.report_failure(proxy)
            raise _RetryableCheckError(f"invalid_json_http_{status}") from exc

        outcome, detail = parse_graph_picture_payload(payload, status)
        if outcome == CheckOutcome.ERROR and detail and detail.startswith("graph_transient_"):
            self._proxy_pool.report_failure(proxy)
            self._note_rate_limit()
            raise _RetryableCheckError(detail)

        self._proxy_pool.report_success(proxy)
        self._consecutive_rate_limits = 0
        return outcome, detail


def parse_graph_picture_payload(
    payload: Any,
    status_code: int = 200,
) -> tuple[CheckOutcome, str | None]:
    if not isinstance(payload, dict):
        return CheckOutcome.ERROR, f"unexpected_payload_http_{status_code}"

    body = cast(dict[str, Any], payload)

    if "error" in body:
        err_raw = body["error"]
        if isinstance(err_raw, dict):
            error = cast(dict[str, Any], err_raw)
            code: Any = error.get("code")
            if error.get("is_transient") or (
                type(code) is int and code in {1, 2, 4, 17, 32, 341, 613}
            ):
                return CheckOutcome.ERROR, f"graph_transient_{code}"
        return CheckOutcome.DIE, "graph_error"

    data_raw = body.get("data")
    if not isinstance(data_raw, dict):
        return CheckOutcome.DIE, f"no_data_http_{status_code}"

    data = cast(dict[str, Any], data_raw)
    height: Any = data.get("height")
    width: Any = data.get("width")
    url: Any = data.get("url")
    if not (
        type(height) is int
        and type(width) is int
        and height > 0
        and width > 0
        and isinstance(url, str)
        and bool(url.strip())
    ):
        return CheckOutcome.DIE, "missing_avatar_dimensions"

    if data.get("is_silhouette") and _contains_any(
        url.lower(), ("static.xx.fbcdn.net", "/rsrc.php/", "c5yt7c_3z_g")
    ):
        return CheckOutcome.DIE, "silhouette_placeholder"

    return CheckOutcome.LIVE, None


def parse_uid_from_profile_html(body: str) -> str | None:
    for pattern in (
        r"fb://profile/([1-9]\d{0,19})",
        r'"userID"\s*:\s*"([1-9]\d{0,19})"',
        r'"selectedID"\s*:\s*"([1-9]\d{0,19})"',
        r'"entity_id"\s*:\s*"([1-9]\d{0,19})"',
    ):
        match = re.search(pattern, body)
        if match:
            return match.group(1)
    return None


def parse_public_profile_html(body: str) -> tuple[str | None, str | None]:
    return _extract_name(body), _extract_avatar(body)


def _is_login_or_checkpoint_url(url: str) -> bool:
    lowered = url.lower()
    return any(
        marker in lowered
        for marker in (
            "/login",
            "/checkpoint",
            "/recover/",
            "login.php",
            "unsupportedbrowser",
        )
    )


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


def _extract_name(body: str) -> str | None:
    candidate = _meta_content(body, "og:title")
    if candidate is None:
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
        candidate = html.unescape(title.group(1)).strip() if title else ""
        for sep in (
            " - Trang chủ | Facebook",
            " - Home | Facebook",
            " | Facebook",
            " - Facebook",
            " • Facebook",
            " (@",
        ):
            if sep in candidate:
                candidate = candidate.split(sep, 1)[0].strip()

    if candidate and candidate.lower() not in {
        "facebook",
        "log in or sign up",
        "log in to facebook",
        "log into facebook",
        "facebook - log in or sign up",
        "đăng nhập facebook",
        "đăng nhập hoặc đăng ký",
        "facebook - đăng nhập hoặc đăng ký",
        "error",
        "lỗi",
        "page not found",
        "content not found",
        "không tìm thấy trang",
        "checkpoint",
        "security check",
    }:
        return candidate
    return None


def _extract_avatar(body: str) -> str | None:
    image = _meta_content(body, "og:image")
    if (
        image
        and image.startswith(("http://", "https://"))
        and not _contains_any(
            image.lower(),
            (
                "static.xx.fbcdn.net",
                "/rsrc.php/",
                "c5yt7c_3z_g",
                "fb_icon",
                "facebook_icon",
                "fb_logo",
            ),
        )
    ):
        return image
    return None
