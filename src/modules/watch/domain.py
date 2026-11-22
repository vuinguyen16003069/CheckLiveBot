from __future__ import annotations

import html
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from src.modules.bulk.domain import extract_uid


class WatchStatus(StrEnum):
    DIE = "DIE"
    LIVE = "LIVE"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class WatchItem(BaseModel):
    id: int
    chat_id: int
    owner_user_id: int
    uid: str
    note: str
    price: str
    faq: str = ""
    status: WatchStatus = WatchStatus.DIE
    is_hidden: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    live_detected_at: datetime | None = None
    completed_at: datetime | None = None


class WatchEvent(BaseModel):
    watch_id: int
    chat_id: int
    uid: str
    event_type: str
    detail: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WatchStats(BaseModel):
    total: int = 0
    die: int = 0
    live: int = 0
    done: int = 0
    cancelled: int = 0


class ParsedWatchInput(BaseModel):
    uid: str
    note: str
    price: str
    faq: str = ""


def parse_add_line(raw: str) -> ParsedWatchInput:
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 3:
        raise ValueError(
            "Sai định dạng. Dùng: <code>/add UID | ghi chú | giá [| FAQ]</code>\n"
            "Ví dụ: <code>/add 100012345678901 | Khách A | 50k</code>"
        )
    uid = extract_uid(parts[0])
    if not uid:
        raise ValueError("UID không hợp lệ (cần 5–20 chữ số hoặc link profile.php?id=...).")
    note = parts[1] or "Không có ghi chú"
    price = parts[2] or "0"
    faq = parts[3] if len(parts) >= 4 else ""
    return ParsedWatchInput(uid=uid, note=note, price=price, faq=faq)


def format_duration(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    chunks: list[str] = []
    if days:
        chunks.append(f"{days}d")
    if hours:
        chunks.append(f"{hours}h")
    if minutes:
        chunks.append(f"{minutes}m")
    if secs or not chunks:
        chunks.append(f"{secs}s")
    return " ".join(chunks)


def _status_badge(status: WatchStatus) -> str:
    return {
        WatchStatus.DIE: "🔴 DIE (đang theo dõi)",
        WatchStatus.LIVE: "🟢 LIVE",
        WatchStatus.DONE: "✅ DONE",
        WatchStatus.CANCELLED: "❌ CANCELLED",
    }[status]


def format_watch_card(item: WatchItem, title: str = "📌 Thông tin kèo") -> str:
    uid_text = "••••••••" if item.is_hidden else f"<code>{html.escape(item.uid)}</code>"
    note_text = "<i>(đã ẩn)</i>" if item.is_hidden else html.escape(item.note)
    price_text = "<i>(đã ẩn)</i>" if item.is_hidden else html.escape(item.price)
    lines = [
        f"<b>{html.escape(title)} #{item.id}</b>",
        f"🆔 UID: {uid_text}",
        f"📝 Ghi chú: {note_text}",
        f"💰 Giá: {price_text}",
    ]
    if item.faq:
        faq_text = "<i>(đã ẩn)</i>" if item.is_hidden else html.escape(item.faq)
        lines.append(f"❓ FAQ: {faq_text}")
    lines.append(f"📊 Trạng thái: {_status_badge(item.status)}")
    if item.live_detected_at:
        elapsed = format_duration(item.created_at, item.live_detected_at)
        lines.append(f"⏱ Thời gian về LIVE: <b>{elapsed}</b>")
    return "\n".join(lines)


def format_live_notification(item: WatchItem) -> str:
    return format_watch_card(item, title="🎉 UID ĐÃ VỀ LIVE")


def format_watch_list(
    items: list[WatchItem],
    *,
    status_filter: str,
    page: int,
    total_pages: int,
    total_items: int,
) -> str:
    header = (
        f"📋 <b>Danh sách kèo</b> (Lọc: <b>{html.escape(status_filter)}</b> | "
        f"Trang {page}/{total_pages} | Tổng {total_items})\n"
    )
    if not items:
        return header + "\n📭 Chưa có kèo nào phù hợp."
    rows: list[str] = [header]
    for item in items:
        badge = {
            WatchStatus.DIE: "🔴",
            WatchStatus.LIVE: "🟢",
            WatchStatus.DONE: "✅",
            WatchStatus.CANCELLED: "❌",
        }[item.status]
        uid_display = "••••" if item.is_hidden else html.escape(item.uid)
        note_display = "(ẩn)" if item.is_hidden else html.escape(item.note)
        price_display = "(ẩn)" if item.is_hidden else html.escape(item.price)
        rows.append(
            f"{badge} <b>#{item.id}</b> <code>{uid_display}</code> — {note_display} | 💰 {price_display}"
        )
    return "\n".join(rows)


def format_watch_stats(stats: WatchStats) -> str:
    return (
        "📊 <b>Thống kê kèo trong chat</b>\n\n"
        f"• Tổng số kèo: <b>{stats.total}</b>\n"
        f"• 🔴 Đang theo dõi (DIE): <b>{stats.die}</b>\n"
        f"• 🟢 Đã về LIVE: <b>{stats.live}</b>\n"
        f"• ✅ Đã Done: <b>{stats.done}</b>\n"
        f"• ❌ Đã Hủy: <b>{stats.cancelled}</b>"
    )


def format_watch_history(events: list[WatchEvent]) -> str:
    if not events:
        return "📜 <b>Lịch sử gần đây</b>\n\nChưa có sự kiện nào."
    lines = ["📜 <b>Lịch sử biến động gần đây</b>\n"]
    for ev in events:
        ts = ev.created_at.strftime("%m/%d %H:%M")
        detail = f" — {html.escape(ev.detail)}" if ev.detail else ""
        lines.append(
            f"• <code>{ts}</code> | <b>#{ev.watch_id}</b> <code>{html.escape(ev.uid)}</code> "
            f"[{html.escape(ev.event_type)}]{detail}"
        )
    return "\n".join(lines)
