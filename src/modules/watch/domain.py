from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

from pydantic import BaseModel, Field

from src.modules.bulk.domain import extract_uid

_VN_TZ = timezone(timedelta(hours=7))


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
    """Single canonical format: UID | Ghi chú | Giá | FAQ (only UID is required)."""
    text = raw.strip()
    if not text:
        raise ValueError(
            "Thiếu dữ liệu. Dùng: <code>/add UID | Ghi chú | Giá | FAQ</code>\n"
            "Hoặc gọn: <code>/add 100045216769361</code>"
        )

    parts = [part.strip() for part in text.split("|")]
    uid = extract_uid(parts[0])
    if not uid:
        raise ValueError("UID không hợp lệ (cần 5–20 chữ số hoặc link Facebook cá nhân).")

    note = parts[1] if len(parts) >= 2 and parts[1] else "Không có ghi chú"
    price = parts[2] if len(parts) >= 3 and parts[2] else "0"
    faq = parts[3] if len(parts) >= 4 else ""
    return ParsedWatchInput(uid=uid, note=note, price=price, faq=faq)


def format_duration(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    chunks: list[str] = []
    if days:
        chunks.append(f"{days} ngày")
    if hours:
        chunks.append(f"{hours} giờ")
    if minutes:
        chunks.append(f"{minutes} phút")
    if not chunks:
        chunks.append(f"{secs} giây")
    return " ".join(chunks)


def _format_vn_time(dt: datetime) -> str:
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(_VN_TZ).strftime("%H:%M:%S %d/%m/%Y")


def _format_price(raw_price: str) -> str:
    cleaned = raw_price.strip()
    if not cleaned:
        return "0đ"
    if cleaned.isdigit():
        num = int(cleaned)
        return f"{num:,}đ".replace(",", ".") if num >= 1000 else f"{num}đ"
    return cleaned


def _progress_label(status: WatchStatus) -> str:
    return {
        WatchStatus.DIE: "🔴 Đang theo dõi chờ LIVE ✅",
        WatchStatus.LIVE: "✅ Đã LIVE lại / Hoàn tất ✅",
        WatchStatus.DONE: "✅ Đã Done kèo ✅",
        WatchStatus.CANCELLED: "❌ Đã Hủy kèo",
    }[status]


def _spoiler(html_text: str, hidden: bool) -> str:
    return f"<tg-spoiler>{html_text}</tg-spoiler>" if hidden else html_text


def format_watch_card(item: WatchItem, title: str | None = None) -> str:
    if title is None:
        header = (
            f"🔔 🟢 <b>THÔNG BÁO TÀI KHOẢN ĐÃ LIVE!</b> <i>(#{item.id})</i>"
            if item.status == WatchStatus.LIVE
            else f"✅ <b>Đã thêm UID thành công!</b> <i>(#{item.id})</i>"
        )
    else:
        header = f"<b>{html.escape(title)}</b> <i>(#{item.id})</i>"

    uid_val = (
        f"<tg-spoiler>{html.escape(item.uid)}</tg-spoiler>"
        if item.is_hidden
        else f"<code>{html.escape(item.uid)}</code>"
    )
    note_val = _spoiler(html.escape(item.note), item.is_hidden)
    price_val = _spoiler(html.escape(_format_price(item.price)), item.is_hidden)

    if item.status == WatchStatus.LIVE:
        uid_line = f"✅ <b>UID</b> {uid_val} - <b>đã Sống lại</b> ✅"
    elif item.status == WatchStatus.DIE:
        uid_line = f"❌ <b>UID</b> {uid_val} - <b>DIE</b> 💀"
    elif item.status == WatchStatus.DONE:
        uid_line = f"✅ <b>UID</b> {uid_val} - <b>DONE</b>"
    else:
        uid_line = f"❌ <b>UID</b> {uid_val} - <b>ĐÃ HỦY</b>"

    lines = [header, "", uid_line]
    lines.append(f"📝 <b>Ghi chú:</b> {note_val}")
    lines.append(f"💰 <b>Giá:</b> {price_val}")

    if item.faq:
        faq_raw = (
            f"{html.escape(item.faq)} → LIVE ✅"
            if item.status == WatchStatus.LIVE
            else html.escape(item.faq)
        )
        lines.append(f"⚠️ <b>FAQ:</b> {_spoiler(faq_raw, item.is_hidden)}")

    lines.append(f"🔄 <b>Tiến trình:</b> {_progress_label(item.status)}")
    display_time = item.live_detected_at or item.last_checked_at or item.created_at
    lines.append(f"🕒 <b>Thời gian:</b> {_spoiler(_format_vn_time(display_time), item.is_hidden)}")

    if item.live_detected_at:
        elapsed = format_duration(item.created_at, item.live_detected_at)
        lines.append(f"⏱ <b>Thời Gian Xử Lý:</b> {_spoiler(elapsed, item.is_hidden)}")

    return "\n".join(lines)


def format_live_notification(item: WatchItem) -> str:
    return format_watch_card(item, title="🔔 🟢 THÔNG BÁO TÀI KHOẢN ĐÃ LIVE!")


def format_watch_list(
    items: list[WatchItem],
    *,
    status_filter: str,
    page: int,
    total_pages: int,
    total_items: int,
    bot_username: str | None = None,
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
        escaped_uid = html.escape(item.uid)
        if bot_username:
            uid_display = (
                f'<a href="https://t.me/{html.escape(bot_username)}?start=w_{item.id}">'
                f"{escaped_uid}</a>"
            )
        else:
            uid_display = f"<code>{escaped_uid}</code>"
        note_display = html.escape(item.note)
        price_display = html.escape(_format_price(item.price))
        faq_part = f" | ⚠️ {html.escape(item.faq)}" if item.faq else ""
        rows.append(
            f"{badge} <b>#{item.id}</b> {uid_display} — {note_display} | 💰 {price_display}{faq_part}"
        )
    if bot_username:
        rows.append("\n<i>💡 Chạm vào UID để xem chi tiết kèo.</i>")
    return "\n".join(rows)


def format_welcome_dashboard(stats: WatchStats) -> str:
    summary = (
        f"🔴 Đang chờ: <b>{stats.die}</b> | "
        f"🟢 LIVE: <b>{stats.live}</b> | "
        f"✅ Done: <b>{stats.done}</b> | "
        f"📦 Tổng: <b>{stats.total}</b>"
    )
    return (
        "👋 <b>Chào mừng đến với CheckLiveBot</b>\n\n"
        f"📊 {summary}\n\n"
        "✨ <b>Dùng nhanh:</b>\n"
        "• Chọn 1-4 nút ở dưới: <b>Lên kèo</b>, <b>Danh sách</b>, <b>Check nhanh</b>, <b>Thống kê</b>\n"
        "• Hoặc dán thẳng <b>UID / link Facebook / nhiều dòng UID / file .txt</b>"
    )


def format_watch_stats(stats: WatchStats) -> str:
    return (
        "📊 <b>Thống kê kèo trong chat</b>\n\n"
        f"• Tổng số kèo: <b>{stats.total}</b>\n"
        f"• 🔴 Đang theo dõi (DIE): <b>{stats.die}</b>\n"
        f"• 🟢 Đã về LIVE: <b>{stats.live}</b>\n"
        f"• ✅ Đã Done: <b>{stats.done}</b>\n"
        f"• ❌ Đã Hủy: <b>{stats.cancelled}</b>"
    )
