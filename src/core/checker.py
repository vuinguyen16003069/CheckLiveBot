from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from curl_cffi.requests import AsyncSession

from src.core.ports import CheckOutcome, UidCheckResult

logger = structlog.get_logger(__name__)

FB_GRAPH_PICTURE_URL = "https://graph.facebook.com/{uid}/picture?type=normal&redirect=false"
_PLACEHOLDER_URL_MARKERS = ("static.xx.fbcdn.net", "/rsrc.php/", "C5yt7C_3z_G")


class _RetryableCheckError(Exception):
    """Raised on network errors, timeouts, 403/429, or 5xx to trigger retry + proxy rotation."""


class ProxyPool:
    def __init__(self, proxies: Sequence[str] | None = None) -> None:
        self._proxies = [p.strip() for p in (proxies or []) if p.strip()]
        self._cursor = 0

    def next_proxy(self) -> str | None:
        if not self._proxies:
            return None
        proxy = self._proxies[self._cursor % len(self._proxies)]
        self._cursor = (self._cursor + 1) % len(self._proxies)
        return proxy

    def __len__(self) -> int:
        return len(self._proxies)


class CurlCffiFacebookChecker:
    """Single-method Facebook LIVE/DIE checker via Graph Picture JSON API (no HTML fallback)."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        max_retries: int = 2,
        proxies: Sequence[str] | None = None,
        session: AsyncSession | None = None,
        verify_delay_seconds: float = 0.5,
        max_clients: int = 100,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_retries = max(1, max_retries)
        self._proxy_pool = ProxyPool(proxies)
        self._owns_session = session is None
        self._max_clients = max(10, max_clients)
        self._session: AsyncSession = session or AsyncSession(
            impersonate="chrome",
            max_clients=self._max_clients,
        )
        self._verify_delay = max(0.0, verify_delay_seconds)

    async def check(self, uid: str) -> UidCheckResult:
        url = FB_GRAPH_PICTURE_URL.format(uid=uid)
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                outcome, detail = await self._fetch_graph_once(url)
                return UidCheckResult(
                    uid=uid,
                    outcome=outcome,
                    checked_at=datetime.now(UTC),
                    detail=detail,
                )
            except _RetryableCheckError as exc:
                last_error = exc
                if attempt + 1 < self._max_retries:
                    await asyncio.sleep(min(0.2 * (2**attempt), 1.0))
            except Exception as exc:
                logger.warning("fb_check_unexpected_error", uid=uid, error=str(exc))
                return UidCheckResult(uid=uid, outcome=CheckOutcome.ERROR, detail=str(exc))

        logger.warning("fb_check_retry_exhausted", uid=uid, error=str(last_error))
        return UidCheckResult(uid=uid, outcome=CheckOutcome.ERROR, detail=str(last_error))

    async def verify_live(self, uid: str) -> UidCheckResult:
        """Layer-2 Graph API re-check after a short pause to prevent false DIE -> LIVE alerts."""
        if self._verify_delay > 0:
            await asyncio.sleep(self._verify_delay)
        return await self.check(uid)

    async def _fetch_graph_once(self, url: str) -> tuple[CheckOutcome, str | None]:
        proxy = self._proxy_pool.next_proxy()
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if proxy:
            kwargs["proxy"] = proxy

        try:
            response = await self._session.get(url, **kwargs)
        except Exception as exc:
            raise _RetryableCheckError(f"request_failed: {exc}") from exc

        status_code = int(response.status_code)
        if status_code in (403, 429) or status_code >= 500:
            raise _RetryableCheckError(f"http_{status_code}")

        try:
            payload = response.json()
        except Exception as exc:
            if status_code == 404:
                return CheckOutcome.DIE, "http_404"
            raise _RetryableCheckError(f"invalid_json_http_{status_code}") from exc

        return parse_graph_picture_payload(payload, status_code)

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()


def parse_graph_picture_payload(
    payload: Any,
    status_code: int = 200,
) -> tuple[CheckOutcome, str | None]:
    if not isinstance(payload, dict):
        return CheckOutcome.ERROR, f"unexpected_payload_http_{status_code}"

    if "error" in payload:
        return CheckOutcome.DIE, "graph_error"

    data = payload.get("data")
    if isinstance(data, dict):
        height = data.get("height")
        width = data.get("width")
        url = data.get("url")
        is_silhouette = bool(data.get("is_silhouette", False))
        if (
            isinstance(height, int)
            and isinstance(width, int)
            and height > 0
            and width > 0
            and isinstance(url, str)
            and bool(url.strip())
        ):
            lower_url = url.lower()
            if is_silhouette and any(m in lower_url for m in _PLACEHOLDER_URL_MARKERS):
                return CheckOutcome.DIE, "silhouette_placeholder"
            return CheckOutcome.LIVE, None
        return CheckOutcome.DIE, "missing_avatar_dimensions"

    return CheckOutcome.DIE, f"no_data_http_{status_code}"
