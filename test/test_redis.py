"""Upstash Redis Management & Diagnostic Suite for CheckLiveBot."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Đảm bảo import được module src từ thư mục gốc khi chạy trực tiếp bằng nút Run
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
from redis.asyncio import Redis  # noqa: E402

from src.core.config import Settings  # noqa: E402
from src.core.redis import create_redis_client  # noqa: E402

# ANSI Colors cho giao diện Terminal
C_CYAN = "\033[96m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_WHITE = "\033[97m"
C_BOLD = "\033[1m"
C_GRAY = "\033[90m"
C_RESET = "\033[0m"


async def ainput(prompt: str = "") -> str:
    """Đọc input người dùng từ terminal trong môi trường async non-blocking."""
    return await asyncio.to_thread(input, prompt)


async def get_value_by_type(r: Redis, key: str, key_type: str, full: bool = False) -> Any:
    """Đọc dữ liệu theo từng kiểu dữ liệu Redis (string, hash, list, set, zset)."""
    if key_type == "string":
        raw = await r.get(key)
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                return json.loads(raw)
            if not full and len(raw) > 200:
                return raw[:200] + "... (truncated)"
        return raw

    if key_type == "hash":
        return await r.hgetall(key)
    if key_type == "list":
        return await r.lrange(key, 0, -1)
    if key_type == "set":
        members = cast(set[str], await r.smembers(key))
        return sorted(members)
    if key_type == "zset":
        return await r.zrange(key, 0, -1, withscores=True)

    return f"({key_type})"


async def get_all_keys(r: Redis, pattern: str = "*") -> list[str]:
    """Lấy danh sách tất cả các key theo pattern và sắp xếp alphabet."""
    iterator = cast(AsyncIterator[str], r.scan_iter(match=pattern, count=500))
    keys = [k async for k in iterator]
    return sorted(keys)


async def get_key_snapshot(r: Redis, key: str, full: bool = False) -> dict[str, Any]:
    """Chụp snapshot trạng thái của 1 key: kiểu dữ liệu, TTL và giá trị."""
    key_type = str(await r.type(key))
    ttl = await r.ttl(key)
    val = await get_value_by_type(r, key, key_type, full=full)
    return {"type": key_type, "ttl": ttl, "value": val}


def build_key_groups(keys: list[str], prefix: str = "checklivebot") -> dict[str, list[str]]:
    """Phân nhóm key rõ ràng theo prefix:subprefix (VD: checklivebot:watch, arq, fsm)."""
    groups: dict[str, list[str]] = {}
    for k in keys:
        parts = k.split(":")
        grp = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 and parts[0] == prefix else parts[0]
        groups.setdefault(grp, []).append(k)
    return groups


async def show_db_capacity(r: Redis, prefix: str) -> None:
    """Xem dung lượng và bộ nhớ của Upstash Redis."""
    print(f"\n{C_BOLD}{C_WHITE}=== UPSTASH REDIS CAPACITY ==={C_RESET}\n")
    try:
        info: dict[str, Any] = await r.info()
        keys = await get_all_keys(r)

        print(f"{C_CYAN}💾 Dung Lượng Lưu Trữ & Bộ Nhớ Redis:{C_RESET}")
        print(f"  • Tổng số Keys:       {C_YELLOW}{len(keys)}{C_RESET}")

        used_mem = info.get("used_memory_human", "N/A")
        max_mem = info.get("maxmemory_human", "N/A")
        print(f"  • RAM đã dùng:        {C_YELLOW}{used_mem}{C_RESET}")
        print(f"  • RAM tối đa:         {C_YELLOW}{max_mem}{C_RESET}")

        used_bytes = float(info.get("used_memory") or 0)
        max_bytes = float(info.get("maxmemory") or 0)
        if max_bytes > 0:
            pct = (used_bytes / max_bytes) * 100
            free_mb = max(0.0, (max_bytes - used_bytes) / (1024 * 1024))
            print(
                f"  • Tỷ lệ RAM:          {C_GREEN}{pct:.4f}%{C_RESET} "
                f"(Còn trống: {C_GREEN}{free_mb:.2f} MB{C_RESET})"
            )

        total_data_size = info.get("total_data_size")
        max_data_size = info.get("max_data_size")
        total_data_size_human = info.get("total_data_size_human", "N/A")
        max_data_size_human = info.get("max_data_size_human", "N/A")
        if total_data_size_human != "N/A":
            print(f"  • Data size đã dùng:  {C_YELLOW}{total_data_size_human}{C_RESET}")
            print(f"  • Data size tối đa:   {C_YELLOW}{max_data_size_human}{C_RESET}")
        if total_data_size and max_data_size:
            try:
                used = float(total_data_size)
                max_d = float(max_data_size)
                if max_d > 0:
                    percent = (used / max_d) * 100
                    free = 100.0 - percent
                    print(
                        f"  • Tỷ lệ Data size:    {C_GREEN}{percent:.4f}%{C_RESET} "
                        f"(Còn trống: {C_GREEN}{free:.4f}%{C_RESET})"
                    )
            except (ValueError, TypeError):
                pass

        print(f"  • Server Redis:       {C_GRAY}{info.get('redis_version', 'N/A')}{C_RESET}")
        print(f"  • Uptime:             {C_GRAY}{info.get('uptime_in_days', 0)} ngày{C_RESET}\n")

        groups = build_key_groups(keys, prefix=prefix)
        if groups:
            print(f"{C_CYAN}🔑 Phân Bổ Nhóm Key Trong Redis:{C_RESET}")
            for grp, grp_keys in sorted(groups.items()):
                print(f"  • Nhóm {C_YELLOW}{grp}:*{C_RESET}: {len(grp_keys)} keys")
        print()
    except Exception as exc:
        print(f"{C_RED}❌ Lỗi khi lấy thông tin Redis: {exc}{C_RESET}\n")


async def fetch_dump_dict(r: Redis, pattern: str = "*", full: bool = False) -> dict[str, Any]:
    """Lấy dữ liệu snapshot của các key theo pattern dưới dạng dictionary."""
    keys = await get_all_keys(r, pattern)
    dump: dict[str, Any] = {}
    for k in keys:
        dump[k] = await get_key_snapshot(r, k, full=full)
    return dump


async def show_json_dump(r: Redis, pattern: str = "*", full: bool = False) -> None:
    """Hiển thị dữ liệu Redis dưới dạng JSON formatted ra terminal."""
    print(f"\n{C_BOLD}{C_WHITE}=== UPSTASH REDIS JSON DUMP (Pattern: {pattern}) ==={C_RESET}\n")
    dump = await fetch_dump_dict(r, pattern=pattern, full=full)
    print(json.dumps(dump, indent=2, ensure_ascii=False))
    print(f"\n{C_GREEN}✓ Đã lấy {len(dump)} key từ Redis.{C_RESET}\n")


def save_dump_file(filename: str, data: dict[str, Any]) -> str:
    """Ghi dữ liệu JSON ra tệp tin."""
    target = Path(filename).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return str(target)


async def export_db_to_json(
    r: Redis,
    filename: str | None = None,
    pattern: str = "*",
) -> str:
    """Xuất toàn bộ dữ liệu từ Redis ra tệp tin JSON."""
    if not filename:
        filename = f"redis_dump_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    if not filename.endswith(".json"):
        filename += ".json"

    print(f"{C_YELLOW}⏳ Đang tải toàn bộ dữ liệu từ Redis...{C_RESET}")
    dump = await fetch_dump_dict(r, pattern=pattern, full=True)
    target_path = await asyncio.to_thread(save_dump_file, filename, dump)

    print(f"{C_GREEN}✓ Đã xuất thành công {len(dump)} key ra file JSON:{C_RESET}")
    print(f"  📁 {C_BOLD}{C_CYAN}{target_path}{C_RESET}\n")
    return target_path


async def export_group_to_json(r: Redis, group_name: str, keys: list[str]) -> str:
    """Xuất một nhóm key cụ thể ra file JSON."""
    default_filename = (
        f"redis_{group_name.replace(':', '_')}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    )
    print(f"{C_YELLOW}⏳ Đang xuất nhóm '{group_name}:*' ({len(keys)} key) ra file...{C_RESET}")
    dump = await fetch_dump_dict(r, pattern=f"{group_name}:*", full=True)
    target_path = await asyncio.to_thread(save_dump_file, default_filename, dump)

    print(f"{C_GREEN}✓ Đã xuất thành công nhóm '{group_name}' ({len(keys)} key):{C_RESET}")
    print(f"  📁 {C_BOLD}{C_CYAN}{target_path}{C_RESET}\n")
    return target_path


async def delete_key_by_name(r: Redis, key_name: str) -> None:
    """Xóa 1 key cụ thể với kiểm tra tồn tại."""
    if not await r.exists(key_name):
        print(f"{C_YELLOW}! Không tìm thấy key '{key_name}' trong Redis.{C_RESET}\n")
        return
    if await r.delete(key_name):
        print(f"{C_GREEN}✓ Đã xóa key '{key_name}'.{C_RESET}\n")


async def delete_group_keys(r: Redis, group_name: str) -> None:
    """Xóa toàn bộ các key trong nhóm group_name:*."""
    keys = await get_all_keys(r, f"{group_name}:*")
    if not keys:
        print(f"{C_YELLOW}! Không có key nào trong nhóm '{group_name}:*'.{C_RESET}\n")
        return
    deleted = await r.delete(*keys)
    print(f"{C_GREEN}✓ Đã xóa {deleted} key trong nhóm '{group_name}:*'.{C_RESET}\n")


async def flush_all_database(r: Redis) -> None:
    """Xóa sạch toàn bộ Redis DB."""
    await r.flushdb()
    print(f"{C_GREEN}✓ Đã xóa sạch toàn bộ Redis DB.{C_RESET}\n")


async def show_group_details(r: Redis, group_name: str, keys: list[str]) -> None:
    """Xem chi tiết một nhóm key, hỗ trợ xuất JSON hoặc xóa nhóm."""
    print(f"\n{C_BOLD}{C_WHITE}=== CHI TIẾT NHÓM: {group_name}:* ({len(keys)} key) ==={C_RESET}\n")
    dump = await fetch_dump_dict(r, pattern=f"{group_name}:*", full=False)
    print(json.dumps(dump, indent=2, ensure_ascii=False))
    print(f"\n{C_GREEN}✓ Đã xem xong nhóm '{group_name}'.{C_RESET}")

    sub_act = (
        (
            await ainput(
                f"\n{C_CYAN}Tùy chọn: [e] Xuất JSON nhóm này | [d] Xóa nhóm | [Enter] Bỏ qua: {C_RESET}"
            )
        )
        .strip()
        .lower()
    )
    if sub_act == "e":
        await export_group_to_json(r, group_name, keys)
    elif sub_act == "d":
        conf = (
            (
                await ainput(
                    f"{C_RED}⚠ Xác nhận xóa {len(keys)} key trong nhóm '{group_name}:*'? "
                    f"(gõ 'y' để xác nhận): {C_RESET}"
                )
            )
            .strip()
            .lower()
        )
        if conf == "y":
            await delete_group_keys(r, group_name)


async def run_interactive_menu(r: Redis, prefix: str) -> None:
    """Giao diện CLI tương tác đa tính năng."""
    try:
        if not await r.ping():
            print(f"{C_RED}❌ Không thể kết nối tới Redis! Kiểm tra REDIS_URL trong .env{C_RESET}")
            return
    except Exception as exc:
        print(f"{C_RED}❌ Lỗi kết nối Redis: {exc}{C_RESET}")
        return

    while True:
        keys = await get_all_keys(r)
        groups = build_key_groups(keys, prefix=prefix)
        group_list = sorted(groups.keys())

        print(f"\n{C_BOLD}{C_CYAN}╔════════════════════════════════════════════════════╗{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}║       CHECKLIVEBOT — REDIS MANAGER & SUITE         ║{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}╚════════════════════════════════════════════════════╝{C_RESET}")
        print(f" {C_WHITE}1. Xem dung lượng & thống kê DB (Capacity & Stats){C_RESET}")
        print(f" {C_GREEN}2. Xuất toàn bộ Redis ra file .json (Export DB){C_RESET}")
        print(f" {C_WHITE}3. Xem toàn bộ JSON Dump (Print JSON Snapshot){C_RESET}")
        print(f" {C_RED}4. Xóa key theo tên (Delete key by name){C_RESET}")
        print(f" {C_RED}5. Xóa toàn bộ DB (Flush entire DB){C_RESET}")

        start_opt = 6
        for idx, grp in enumerate(group_list):
            print(
                f" {C_CYAN}{start_opt + idx}. Xem nhóm: {grp}:* ({len(groups[grp])} keys){C_RESET}"
            )

        print(f" {C_GRAY}0. Thoát{C_RESET}")

        try:
            choice = (await ainput(f"\n{C_CYAN}Nhập lựa chọn của bạn: {C_RESET}")).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{C_GRAY}Tạm biệt!{C_RESET}")
            break

        if choice in ("0", "q", "exit", "quit"):
            print(f"{C_GRAY}Tạm biệt!{C_RESET}")
            break
        if choice == "1":
            await show_db_capacity(r, prefix)
        elif choice == "2":
            fname = (
                await ainput(
                    f"{C_CYAN}Nhập tên file để lưu (Enter để tự đặt tên theo ngày giờ): {C_RESET}"
                )
            ).strip()
            await export_db_to_json(r, fname or None)
        elif choice == "3":
            await show_json_dump(r)
        elif choice == "4":
            k = (await ainput(f"{C_CYAN}Nhập tên key cần xóa: {C_RESET}")).strip()
            if k:
                conf = (
                    (await ainput(f"{C_YELLOW}Xác nhận xóa '{k}'? (gõ 'y' để xác nhận): {C_RESET}"))
                    .strip()
                    .lower()
                )
                if conf == "y":
                    await delete_key_by_name(r, k)
        elif choice == "5":
            conf = (
                await ainput(
                    f"{C_RED}⚠ BẠN CHẮC CHẮN MUỐN XÓA TOÀN BỘ DB? (gõ 'flush' để xác nhận): {C_RESET}"
                )
            ).strip()
            if conf == "flush":
                await flush_all_database(r)
        elif choice.isdigit():
            c_int = int(choice)
            if start_opt <= c_int < start_opt + len(group_list):
                grp_name = group_list[c_int - start_opt]
                await show_group_details(r, grp_name, groups[grp_name])
            else:
                print(f"{C_RED}Lựa chọn không hợp lệ!{C_RESET}\n")
        else:
            print(f"{C_RED}Lựa chọn không hợp lệ!{C_RESET}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="CheckLiveBot Upstash Redis Manager & Suite")
    parser.add_argument("--info", action="store_true", help="Xem dung lượng và thống kê DB")
    parser.add_argument("--dump", action="store_true", help="Hiển thị JSON Dump toàn bộ Redis")
    parser.add_argument("--export", nargs="?", const="default", help="Xuất Redis ra file JSON")
    parser.add_argument("--pattern", default="*", help="Pattern filter khi dump/export")
    args = parser.parse_args()

    settings = Settings()
    r = create_redis_client(settings.redis_url)
    try:
        if args.info:
            await show_db_capacity(r, settings.redis_key_prefix)
        elif args.dump:
            await show_json_dump(r, pattern=args.pattern)
        elif args.export:
            fname = None if args.export == "default" else args.export
            await export_db_to_json(r, fname, pattern=args.pattern)
        else:
            await run_interactive_menu(r, settings.redis_key_prefix)
    finally:
        await r.aclose()


# === Pytest Unit Tests (chạy tự động offline bằng fakeredis) ===


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

    # Truncated vs full string snapshot + all Redis data types
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
    await r.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print(f"\n{C_GRAY}Tạm biệt!{C_RESET}")
