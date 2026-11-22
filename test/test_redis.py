from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
from redis.asyncio import Redis  # noqa: E402

from src.core.config import Settings  # noqa: E402
from src.core.redis import create_redis_client  # noqa: E402


def _fmt_bytes(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.2f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024  # type: ignore[assignment]
    return f"{num_bytes} B"


async def collect_redis_report(redis: Redis, prefix: str = "checklivebot") -> dict[str, Any]:
    ping_ok = bool(await redis.ping())  # type: ignore[misc]
    dbsize = int(await redis.dbsize())

    used_bytes = 0
    max_bytes = 0
    used_human = "N/A"
    max_human = "Không giới hạn / N/A"
    try:
        mem_info = await redis.info("memory")
        used_bytes = int(mem_info.get("used_memory", 0) or 0)
        max_bytes = int(mem_info.get("maxmemory", 0) or 0)
        used_human = str(mem_info.get("used_memory_human") or _fmt_bytes(used_bytes))
        if max_bytes > 0:
            max_human = str(mem_info.get("maxmemory_human") or _fmt_bytes(max_bytes))
    except Exception:
        pass

    remaining_bytes = max(0, max_bytes - used_bytes) if max_bytes > 0 else None
    used_percent = round((used_bytes / max_bytes) * 100, 2) if max_bytes > 0 else None

    keys: list[str] = [str(k) async for k in redis.scan_iter(match="*", count=500)]
    groups: Counter[str] = Counter()
    watches: list[dict[str, Any]] = []

    for key in sorted(keys):
        if key.startswith(f"{prefix}:watch:"):
            groups["watch"] += 1
            raw = await redis.get(key)
            if raw:
                watches.append(json.loads(raw))
        elif key.startswith(f"{prefix}:watches:"):
            groups["watches_index"] += 1
        elif key.startswith(f"{prefix}:events:"):
            groups["events"] += 1
        elif key.startswith(f"{prefix}:check_cache:"):
            groups["check_cache"] += 1
        elif key.startswith(f"{prefix}:lock:"):
            groups["lock"] += 1
        elif key.startswith(f"{prefix}:ratelimit:"):
            groups["ratelimit"] += 1
        elif key.startswith("fsm:"):
            groups["fsm"] += 1
        elif key.startswith("arq:"):
            groups["arq"] += 1
        else:
            groups["other"] += 1

    return {
        "ping": ping_ok,
        "dbsize": dbsize,
        "used_bytes": used_bytes,
        "used_human": used_human,
        "max_bytes": max_bytes,
        "max_human": max_human,
        "remaining_human": _fmt_bytes(remaining_bytes) if remaining_bytes is not None else "N/A",
        "used_percent": used_percent,
        "groups": dict(groups),
        "watches": watches,
    }


def _print_report(report: dict[str, Any]) -> None:
    print("\n================ REDIS REPORT ================")
    print(f"PING            : {report['ping']}")
    print(f"Tổng số keys    : {report['dbsize']}")
    print(f"RAM đã dùng     : {report['used_human']}")
    print(f"RAM giới hạn    : {report['max_human']}")
    pct = f" ({report['used_percent']}%)" if report["used_percent"] is not None else ""
    print(f"RAM còn lại     : {report['remaining_human']}{pct}")
    print(f"Phân loại keys  : {report['groups'] or 'Trống'}")
    watches: list[dict[str, Any]] = report["watches"]
    print(f"Số kèo đang lưu : {len(watches)}")
    for w in watches[:20]:
        print(
            f"  - #{w.get('id')} | chat={w.get('chat_id')} | uid={w.get('uid')} "
            f"| {w.get('status')} | {w.get('note')} | {w.get('price')}"
        )
    print("==============================================\n")


@pytest.mark.asyncio
async def test_redis_report_offline_fakeredis() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.set("checklivebot:check_cache:20531316728", "LIVE", ex=600)
    await redis.set(
        "checklivebot:watch:1",
        json.dumps(
            {
                "id": 1,
                "chat_id": 100,
                "uid": "100099999999991",
                "status": "DIE",
                "note": "Test",
                "price": "50k",
            }
        ),
    )

    report = await collect_redis_report(redis, prefix="checklivebot")
    assert report["ping"] is True
    assert report["dbsize"] == 2
    assert report["groups"]["watch"] == 1
    assert report["groups"]["check_cache"] == 1
    assert len(report["watches"]) == 1
    await redis.aclose()


@pytest.mark.asyncio
async def test_live_redis_memory_and_data() -> None:
    """Chạy `uv run pytest test/test_redis_inspect.py -s` để xem RAM còn lại & dữ liệu thật trên Redis."""
    settings = Settings()
    redis = create_redis_client(settings.effective_redis_url)
    try:
        report = await collect_redis_report(redis, prefix=settings.redis_key_prefix)
    except Exception as exc:
        pytest.skip(f"Không kết nối được Redis thật ({exc})")
    finally:
        await redis.aclose()

    _print_report(report)
    assert report["ping"] is True
    assert report["dbsize"] >= 0


if __name__ == "__main__":

    async def _run_cli() -> None:
        settings = Settings()
        redis = create_redis_client(settings.effective_redis_url)
        try:
            report = await collect_redis_report(redis, prefix=settings.redis_key_prefix)
            _print_report(report)
        finally:
            await redis.aclose()

    asyncio.run(_run_cli())
