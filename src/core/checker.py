from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Sequence
from typing import Any, cast

from curl_cffi.requests import AsyncSession

from src.core.ports import CheckOutcome, UidCheckResult

logger = logging.getLogger(__name__)

_TRANSIENT_GRAPH_ERROR_CODES = frozenset({1, 2, 4, 17, 32, 341, 613})
_SILHOUETTE_MARKERS = ("static.xx.fbcdn.net", "/rsrc.php/", "c5yt7c_3z_g")


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
                type(code) is int and code in _TRANSIENT_GRAPH_ERROR_CODES
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
    if data.get("is_silhouette") and any(marker in url_lower for marker in _SILHOUETTE_MARKERS):
        return CheckOutcome.DIE, "silhouette_placeholder"

    return CheckOutcome.LIVE, None
