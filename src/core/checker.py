from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import cycle
from typing import Any, cast

import structlog
from curl_cffi.requests import AsyncSession

from src.core.ports import CheckOutcome, UidCheckResult

logger = structlog.get_logger(__name__)


class _RetryableCheckError(Exception):
    pass


class ProxyPool:
    def __init__(self, proxies: Sequence[str] | None = None) -> None:
        self._proxies = [p.strip() for p in proxies or [] if p.strip()]
        self._iterator = cycle(self._proxies)

    def next_proxy(self) -> str | None:
        return next(self._iterator, None)

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
        self._max_retries = max(1, max_retries)
        self._verify_delay = max(0.0, verify_delay_seconds)
        self._proxy_pool = ProxyPool(proxies)
        self._owns_session = session is None
        self._session: AsyncSession[Any] = session or AsyncSession(
            impersonate="chrome",
            max_clients=max(10, max_clients),
        )

    async def __aenter__(self) -> CurlCffiFacebookChecker:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def check(self, uid: str) -> UidCheckResult:
        url = f"https://graph.facebook.com/{uid}/picture?type=normal&redirect=false"
        last_error: _RetryableCheckError | None = None

        for attempt in range(self._max_retries):
            try:
                outcome, detail = await self._fetch_graph_once(url)
            except _RetryableCheckError as exc:
                last_error = exc
                if attempt + 1 < self._max_retries:
                    await asyncio.sleep(min(0.2 * 2**attempt, 1.0))
            except Exception as exc:
                logger.warning("fb_check_unexpected_error", uid=uid, error=str(exc))
                return self._result(uid, CheckOutcome.ERROR, str(exc))
            else:
                return self._result(uid, outcome, detail)

        logger.warning("fb_check_retry_exhausted", uid=uid, error=str(last_error))
        return self._result(uid, CheckOutcome.ERROR, str(last_error))

    async def verify_live(self, uid: str) -> UidCheckResult:
        await asyncio.sleep(self._verify_delay)
        return await self.check(uid)

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()

    async def _fetch_graph_once(self, url: str) -> tuple[CheckOutcome, str | None]:
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if proxy := self._proxy_pool.next_proxy():
            kwargs["proxy"] = proxy

        try:
            response: Any = await self._session.get(url, **kwargs)
        except Exception as exc:
            raise _RetryableCheckError(f"request_failed: {exc}") from exc

        status = int(response.status_code)
        if status in (403, 429) or status >= 500:
            raise _RetryableCheckError(f"http_{status}")

        try:
            payload: Any = response.json()
        except Exception as exc:
            if status == 404:
                return CheckOutcome.DIE, "http_404"
            raise _RetryableCheckError(f"invalid_json_http_{status}") from exc

        return parse_graph_picture_payload(payload, status)

    @staticmethod
    def _result(uid: str, outcome: CheckOutcome, detail: str | None) -> UidCheckResult:
        return UidCheckResult(
            uid=uid,
            outcome=outcome,
            checked_at=datetime.now(UTC),
            detail=detail,
        )


def _has_valid_avatar(data: dict[str, Any]) -> bool:
    height, width, url = data.get("height"), data.get("width"), data.get("url")
    return (
        isinstance(height, int)
        and isinstance(width, int)
        and height > 0
        and width > 0
        and isinstance(url, str)
        and bool(url.strip())
    )


def parse_graph_picture_payload(
    payload: Any,
    status_code: int = 200,
) -> tuple[CheckOutcome, str | None]:
    if not isinstance(payload, dict):
        return CheckOutcome.ERROR, f"unexpected_payload_http_{status_code}"

    payload_dict = cast(dict[str, Any], payload)
    if "error" in payload_dict:
        return CheckOutcome.DIE, "graph_error"

    data_raw = payload_dict.get("data")
    if not isinstance(data_raw, dict):
        return CheckOutcome.DIE, f"no_data_http_{status_code}"

    data = cast(dict[str, Any], data_raw)
    if not _has_valid_avatar(data):
        return CheckOutcome.DIE, "missing_avatar_dimensions"

    url_lower = str(data["url"]).lower()
    if data.get("is_silhouette") and any(
        marker in url_lower for marker in ("static.xx.fbcdn.net", "/rsrc.php/", "C5yt7C_3z_G")
    ):
        return CheckOutcome.DIE, "silhouette_placeholder"

    return CheckOutcome.LIVE, None
