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

from curl_cffi.requests import AsyncSession

from src.core.ports import CheckOutcome, UidCheckResult

logger = logging.getLogger(__name__)


class _RetryableCheckError(Exception):
    pass


class ProxyPool:
    """Round-robin proxy pool with automatic short cooldown for failing/rate-limited proxies."""

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
        best_proxy: str | None = None
        earliest_ready = float("inf")
        for offset in range(total):
            idx = (self._cursor + offset) % total
            candidate = self._proxies[idx]
            ready_at = self._cooldown_until.get(candidate, 0.0)
            if ready_at <= now:
                self._cursor = (idx + 1) % total
                return candidate
            if ready_at < earliest_ready:
                earliest_ready = ready_at
                best_proxy = candidate
        if best_proxy is not None:
            self._cursor = (self._proxies.index(best_proxy) + 1) % total
        return best_proxy

    def report_failure(self, proxy: str | None) -> None:
        if not proxy:
            return
        failures = self._failures.get(proxy, 0) + 1
        self._failures[proxy] = failures
        cooldown = min(self._base_cooldown * (2 ** (failures - 1)), self._max_cooldown)
        self._cooldown_until[proxy] = time.monotonic() + cooldown

    def report_success(self, proxy: str | None) -> None:
        if not proxy:
            return
        self._failures.pop(proxy, None)
        self._cooldown_until.pop(proxy, None)

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
            max_clients=max(10, max_clients),
            allow_redirects=False,
            discard_cookies=True,
        )
        self._inflight: dict[str, asyncio.Task[UidCheckResult]] = {}

    async def __aenter__(self) -> CurlCffiFacebookChecker:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def check(self, uid: str) -> UidCheckResult:
        """Check UID with in-flight request coalescing (singleflight, zero stale cache)."""
        task = self._inflight.get(uid)
        if task is None:
            task = asyncio.create_task(self._check_direct(uid))
            self._inflight[uid] = task

            def _cleanup(done_task: asyncio.Task[UidCheckResult]) -> None:
                if self._inflight.get(uid) is done_task:
                    self._inflight.pop(uid, None)

            task.add_done_callback(_cleanup)
        return await asyncio.shield(task)

    async def verify_live(self, uid: str) -> UidCheckResult:
        """Independent layer-2 verification bypassing singleflight."""
        await asyncio.sleep(self._verify_delay)
        return await self._check_direct(uid)

    async def fetch_profile_info(self, uid: str) -> tuple[str | None, str | None]:
        """Best-effort fast fetch of public Facebook name and avatar URL (streams only <head>)."""
        url = f"https://www.facebook.com/profile.php?id={uid}"
        kwargs: dict[str, Any] = {
            "timeout": (self._connect_timeout, self._timeout),
            "allow_redirects": True,
            "stream": True,
            "headers": {
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
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
            response = await self._session.get(url, **kwargs)
            iter_content = getattr(response, "aiter_content", None)
            if callable(iter_content):
                buf = bytearray()
                async for chunk in response.aiter_content():
                    buf.extend(chunk)
                    if (
                        (b"og:title" in buf and b"og:image" in buf)
                        or b"</head>" in buf
                        or len(buf) >= 65536
                    ):
                        break
                body = bytes(buf).decode("utf-8", errors="ignore")
            else:
                raw_text = getattr(response, "text", "")
                body = raw_text if isinstance(raw_text, str) else ""
            if not body:
                return None, None
            return parse_public_profile_html(body)
        except Exception as exc:
            logger.debug("fb_profile_info_failed uid=%s error=%s", uid, exc)
            return None, None
        finally:
            if response is not None:
                aclose = getattr(response, "aclose", None)
                if callable(aclose):
                    with contextlib.suppress(Exception):
                        await response.aclose()

    async def close(self) -> None:
        pending = list(self._inflight.values())
        self._inflight.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._owns_session:
            await self._session.close()

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

    async def _fetch_graph_once(self, url: str) -> tuple[CheckOutcome, str | None]:
        kwargs: dict[str, Any] = {"timeout": (self._connect_timeout, self._timeout)}
        proxy = self._proxy_pool.next_proxy()
        if proxy:
            kwargs["proxy"] = proxy

        try:
            response: Any = await self._session.get(url, **kwargs)
        except Exception as exc:
            self._proxy_pool.report_failure(proxy)
            raise _RetryableCheckError(f"request_failed: {exc}") from exc

        status = int(response.status_code)
        if status in (403, 429) or status >= 500 or 300 <= status < 400:
            self._proxy_pool.report_failure(proxy)
            raise _RetryableCheckError(f"http_{status}")

        try:
            payload: Any = response.json()
        except Exception as exc:
            if status == 404:
                self._proxy_pool.report_success(proxy)
                return CheckOutcome.DIE, "http_404"
            self._proxy_pool.report_failure(proxy)
            raise _RetryableCheckError(f"invalid_json_http_{status}") from exc

        outcome, detail = parse_graph_picture_payload(payload, status)
        if outcome == CheckOutcome.ERROR and detail and detail.startswith("graph_transient_"):
            self._proxy_pool.report_failure(proxy)
            raise _RetryableCheckError(detail)

        self._proxy_pool.report_success(proxy)
        return outcome, detail


def parse_graph_picture_payload(
    payload: Any,
    status_code: int = 200,
) -> tuple[CheckOutcome, str | None]:
    if not isinstance(payload, dict):
        return CheckOutcome.ERROR, f"unexpected_payload_http_{status_code}"

    payload_dict = cast(dict[str, Any], payload)
    if "error" in payload_dict:
        err_raw = payload_dict.get("error")
        if isinstance(err_raw, dict):
            err_dict = cast(dict[str, Any], err_raw)
            code = err_dict.get("code")
            if bool(err_dict.get("is_transient")) or (
                type(code) is int and code in {1, 2, 4, 17, 32, 341, 613}
            ):
                return CheckOutcome.ERROR, f"graph_transient_{code}"
        return CheckOutcome.DIE, "graph_error"

    data_raw = payload_dict.get("data")
    if not isinstance(data_raw, dict):
        return CheckOutcome.DIE, f"no_data_http_{status_code}"

    data = cast(dict[str, Any], data_raw)
    height, width, url = data.get("height"), data.get("width"), data.get("url")
    if not (
        type(height) is int
        and type(width) is int
        and height > 0
        and width > 0
        and isinstance(url, str)
        and bool(url.strip())
    ):
        return CheckOutcome.DIE, "missing_avatar_dimensions"

    url_lower = url.lower()
    if data.get("is_silhouette") and any(
        marker in url_lower for marker in ("static.xx.fbcdn.net", "/rsrc.php/", "c5yt7c_3z_g")
    ):
        return CheckOutcome.DIE, "silhouette_placeholder"

    return CheckOutcome.LIVE, None


def parse_public_profile_html(body: str) -> tuple[str | None, str | None]:
    """Extract (fb_name, avatar_url) from public Facebook profile HTML metadata."""
    name: str | None = None
    og_title = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        body,
        re.IGNORECASE,
    )
    if og_title is None:
        og_title = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
            body,
            re.IGNORECASE,
        )
    if og_title:
        candidate = html.unescape(og_title.group(1)).strip()
    else:
        title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
        candidate = html.unescape(title_m.group(1)).strip() if title_m else ""
        for sep in (" | Facebook", " - Facebook", " • Facebook", " (@"):
            if sep in candidate:
                candidate = candidate.split(sep, 1)[0].strip()

    lowered = candidate.lower()
    if candidate and lowered not in {
        "facebook",
        "log in or sign up",
        "log into facebook",
        "facebook - log in or sign up",
        "error",
        "page not found",
        "content not found",
    }:
        name = candidate

    avatar_url: str | None = None
    og_image = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        body,
        re.IGNORECASE,
    )
    if og_image is None:
        og_image = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            body,
            re.IGNORECASE,
        )
    if og_image:
        raw_img = html.unescape(og_image.group(1)).strip()
        img_lower = raw_img.lower()
        if raw_img.startswith(("http://", "https://")) and not any(
            marker in img_lower
            for marker in ("static.xx.fbcdn.net", "/rsrc.php/", "fb_icon", "c5yt7c_3z_g")
        ):
            avatar_url = raw_img

    return name, avatar_url
