from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import fakeredis.aioredis
import pytest

from src.modules.watch.domain import WatchItem, WatchStatus
from src.modules.watch.service import WatchTtlPolicy
from src.ops.redis_admin import (
    build_key_groups,
    delete_group_keys,
    delete_key_by_name,
    fetch_dump_dict,
    flush_all_database,
    get_all_keys,
    get_key_snapshot,
    save_dump_file,
    sync_existing_ttls,
)


@pytest.mark.asyncio
async def test_redis_manager_helpers_offline(tmp_path: Path) -> None:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.set(
        "checklivebot:watch:1",
        json.dumps({"id": 1, "uid": "100099", "status": "DIE"}),
        ex=3600,
    )
    await r.set("checklivebot:raw:long", "x" * 250)
    await r.hset("checklivebot:chat_uid:100", "100099", "1")
    await r.rpush("checklivebot:list:1", "a", "b")
    await r.sadd("checklivebot:watches:die", "2", "1")
    await r.zadd("checklivebot:watches:chat:100", {"1": 1234.5})

    keys = await get_all_keys(r)
    assert len(keys) == 6

    groups = build_key_groups(keys, prefix="checklivebot")
    assert "checklivebot:watch" in groups
    assert "checklivebot:chat_uid" in groups
    assert "checklivebot:watches" in groups

    short_snap = await get_key_snapshot(r, "checklivebot:raw:long", full=False)
    assert short_snap["value"].endswith("... (truncated)")
    full_snap = await get_key_snapshot(r, "checklivebot:raw:long", full=True)
    assert full_snap["value"] == "x" * 250

    dump = await fetch_dump_dict(r, full=True)
    assert dump["checklivebot:watch:1"]["type"] == "string"
    assert dump["checklivebot:watch:1"]["value"]["uid"] == "100099"
    assert dump["checklivebot:chat_uid:100"]["value"] == {"100099": "1"}
    assert dump["checklivebot:list:1"]["value"] == ["a", "b"]
    assert dump["checklivebot:watches:die"]["value"] == ["1", "2"]
    assert dump["checklivebot:watches:chat:100"]["value"] == [("1", 1234.5)]

    out_file = await asyncio.to_thread(save_dump_file, str(tmp_path / "backup.json"), dump)
    saved_text = await asyncio.to_thread(Path(out_file).read_text, "utf-8")
    assert json.loads(saved_text)["checklivebot:watch:1"]["value"]["uid"] == "100099"

    await delete_key_by_name(r, "checklivebot:raw:long")
    assert await r.exists("checklivebot:raw:long") == 0
    await delete_group_keys(r, "checklivebot:watches")
    assert await get_all_keys(r, "checklivebot:watches:*") == []

    await flush_all_database(r)
    assert await get_all_keys(r) == []

    now = datetime.now(UTC)
    item_cancelled = WatchItem(
        id=10,
        chat_id=100,
        owner_user_id=1,
        uid="100010",
        note="Cancelled",
        price="0",
        status=WatchStatus.CANCELLED,
        created_at=now,
        last_checked_at=now,
        completed_at=now,
    )
    await r.set("checklivebot:seq:watch_id", "10", ex=90 * 86400)
    await r.set("checklivebot:pref:hidden:100", "1", ex=90 * 86400)
    await r.set("checklivebot:watch:10", item_cancelled.model_dump_json(), ex=90 * 86400)
    await r.zadd("checklivebot:watches:chat:100", {"10": now.timestamp()})
    await r.zadd("checklivebot:watches:chat:100:status:CANCELLED", {"10": now.timestamp()})
    await r.hset("checklivebot:chat_uid:100", "100010", "10")
    await r.hset("checklivebot:chat_uid:100", "999999", "999")

    summary = await sync_existing_ttls(r, "checklivebot", WatchTtlPolicy())
    assert summary == {"seq": 1, "pref": 1, "watch": 1, "index": 3, "stale_uids_pruned": 1}
    assert await r.ttl("checklivebot:seq:watch_id") == -1
    assert 364 * 86400 < await r.ttl("checklivebot:pref:hidden:100") <= 365 * 86400
    assert 13 * 86400 < await r.ttl("checklivebot:watch:10") <= 14 * 86400
    assert 13 * 86400 < await r.ttl("checklivebot:watches:chat:100:status:CANCELLED") <= 14 * 86400
    assert 89 * 86400 < await r.ttl("checklivebot:watches:chat:100") <= 90 * 86400
    assert await r.hgetall("checklivebot:chat_uid:100") == {"100010": "10"}
    await r.aclose()
