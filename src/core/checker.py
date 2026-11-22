from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from curl_cffi.requests import AsyncSession
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.core.ports import CheckOutcome, UidCheckResult

logger = structlog.get_logger(__name__)

FB_GRAPH_PICTURE_URL = "https://graph.facebook.com/{uid}/picture?redirect=false"
FB_WEB_PROFILE_URL = "https://www.facebook.com/{username}"

_OG_TITLE_RE = re.compile(
    r'property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_USER_ID_RE = re.compile(r'"(?:userID|entity_id)":"(\d+)"')
_GENERIC_TITLES = {"facebook", "log in or sign up", "log into facebook", "đăng nhập facebook"}


class _RetryableCheckError(Exception):
    """Raised on network errors, timeouts, 429, or 5xx to trigger tenacity retry + proxy rotation."""


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
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        proxies: Sequence[str] | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_retries = max(1, max_retries)
        self._proxy_pool = ProxyPool(proxies)
        self._owns_session = session is None
        self._session: AsyncSession = session or AsyncSession(impersonate="chrome")

    async def check(self, uid: str) -> UidCheckResult:
        is_numeric = uid.isdigit()
        url = (
            FB_GRAPH_PICTURE_URL.format(uid=uid)
            if is_numeric
            else FB_WEB_PROFILE_URL.format(username=uid)
        )
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_retries),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
                retry=retry_if_exception_type(_RetryableCheckError),
                reraise=True,
            ):
                with attempt:
                    outcome, detail = (
                        await self._fetch_graph_once(url)
                        if is_numeric
                        else await self._fetch_username_once(url)
                    )
                    return UidCheckResult(
                        uid=uid,
                        outcome=outcome,
                        checked_at=datetime.now(UTC),
                        detail=detail,
                    )
        except _RetryableCheckError as exc:
            logger.warning("fb_check_retry_exhausted", uid=uid, error=str(exc))
            return UidCheckResult(uid=uid, outcome=CheckOutcome.ERROR, detail=str(exc))
        except Exception as exc:
            logger.warning("fb_check_unexpected_error", uid=uid, error=str(exc))
            return UidCheckResult(uid=uid, outcome=CheckOutcome.ERROR, detail=str(exc))

        return UidCheckResult(uid=uid, outcome=CheckOutcome.ERROR, detail="unknown")

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
        if status_code == 429 or status_code >= 500:
            raise _RetryableCheckError(f"http_{status_code}")

        try:
            payload = response.json()
        except Exception as exc:
            if status_code == 404:
                return CheckOutcome.DIE, "http_404"
            raise _RetryableCheckError(f"invalid_json_http_{status_code}") from exc

        return parse_graph_picture_payload(payload, status_code)

    async def _fetch_username_once(self, url: str) -> tuple[CheckOutcome, str | None]:
        proxy = self._proxy_pool.next_proxy()
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if proxy:
            kwargs["proxy"] = proxy

        try:
            response = await self._session.get(url, **kwargs)
        except Exception as exc:
            raise _RetryableCheckError(f"request_failed: {exc}") from exc

        status_code = int(response.status_code)
        if status_code == 429 or status_code >= 500:
            raise _RetryableCheckError(f"http_{status_code}")
        if status_code == 404:
            return CheckOutcome.DIE, "http_404"

        return parse_web_profile_html(response.text, status_code)

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()


def parse_web_profile_html(
    html_text: str,
    status_code: int = 200,
) -> tuple[CheckOutcome, str | None]:
    if not html_text:
        return CheckOutcome.ERROR, f"empty_html_http_{status_code}"
    uid_match = _USER_ID_RE.search(html_text)
    og_match = _OG_TITLE_RE.search(html_text)
    og_title = og_match.group(1).strip() if og_match else ""
    if uid_match or (og_title and og_title.lower() not in _GENERIC_TITLES):
        return CheckOutcome.LIVE, og_title or None
    return CheckOutcome.DIE, "profile_unavailable"


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
        if (
            isinstance(height, int)
            and isinstance(width, int)
            and height > 0
            and width > 0
            and isinstance(url, str)
            and bool(url.strip())
        ):
            return CheckOutcome.LIVE, None
        return CheckOutcome.DIE, "missing_avatar_dimensions"

    return CheckOutcome.DIE, f"no_data_http_{status_code}"
