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
    text = raw.strip()
    if not text:
        raise ValueError(
            "Thiếu dữ liệu. Dùng: <code>/add UID | ghi chú | giá [| FAQ]</code>\n"
            "Hoặc gọn: <code>/add 100012345678901</code>"
        )

    parts = [part.strip() for part in text.split("|")]

    # Style 1: /add UID | note | price [| faq]
    if len(parts) >= 3:
        uid = extract_uid(parts[0])
        if not uid:
            raise ValueError("UID không hợp lệ (cần 5–20 chữ số hoặc link profile.php?id=...).")
        note = parts[1] or "Không có ghi chú"
        price = parts[2] or "0"
        faq = parts[3] if len(parts) >= 4 else ""
        return ParsedWatchInput(uid=uid, note=note, price=price, faq=faq)

    # Style 2: /add UID note | price  OR  /add UID | note
    if len(parts) == 2:
        first_tokens = parts[0].split(None, 1)
        uid = extract_uid(first_tokens[0]) if first_tokens else None
        if not uid:
            raise ValueError("UID không hợp lệ (cần 5–20 chữ số hoặc link profile.php?id=...).")
        if len(first_tokens) == 2:
            note = first_tokens[1].strip() or "Không có ghi chú"
            price = parts[1] or "0"
        else:
            note = parts[1] or "Không có ghi chú"
            price = "0"
        return ParsedWatchInput(uid=uid, note=note, price=price)

    # Style 3: /add UID [note...]
    tokens = text.split(None, 1)
    uid = extract_uid(tokens[0]) if tokens else None
    if not uid:
        raise ValueError("UID không hợp lệ (cần 5–20 chữ số hoặc link profile.php?id=...).")
    note = tokens[1].strip() if len(tokens) == 2 else "Theo dõi nhanh"
    return ParsedWatchInput(uid=uid, note=note, price="0")


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

    lines = [
        header,
        "",
        uid_line,
        f"📝 <b>Ghi chú:</b> {note_val}",
        f"💰 <b>Giá:</b> {price_val}",
    ]
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
        "👋 <b>Chào mừng đến với CheckLiveBot!</b>\n\n"
        f"📊 {summary}\n\n"
        "✨ <b>Dùng cực nhanh:</b>\n"
        "• Dán thẳng <b>1 UID / link Facebook</b> để check ngay\n"
        "• Dán <b>nhiều dòng UID</b> hoặc gửi <b>file .txt</b> để check hàng loạt\n"
        "• Hoặc chọn nhanh nút bên dưới:"
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


def format_search_results(
    query: str,
    items: list[WatchItem],
    *,
    bot_username: str | None = None,
) -> str:
    header = (
        f"🔍 <b>Kết quả tìm kiếm</b> cho <code>{html.escape(query)}</code> ({len(items)} kèo)\n"
    )
    if not items:
        return header + "\n📭 Không tìm thấy kèo phù hợp trong chat này."
    rows = [header]
    for item in items[:20]:
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
    return "\n".join(rows)
