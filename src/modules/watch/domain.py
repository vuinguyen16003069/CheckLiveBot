from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta, timezone
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
    fb_name: str = ""
    avatar_url: str = ""
    avatar_file_id: str = ""
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


MAX_NOTE_LEN = 400
MAX_PRICE_LEN = 64
MAX_FAQ_LEN = 128


def normalize_watch_fields(
    note: str,
    price: str,
    faq: str,
    *,
    default_note: str = "Không có ghi chú",
) -> tuple[str, str, str]:
    cleaned_note = note.strip() or default_note
    cleaned_price = price.strip() or "0"
    cleaned_faq = faq.strip()
    if cleaned_faq == "-":
        cleaned_faq = ""
    if len(cleaned_note) > MAX_NOTE_LEN:
        raise ValueError(f"Ghi chú tối đa {MAX_NOTE_LEN} ký tự.")
    if len(cleaned_price) > MAX_PRICE_LEN:
        raise ValueError(f"Giá tối đa {MAX_PRICE_LEN} ký tự.")
    if len(cleaned_faq) > MAX_FAQ_LEN:
        raise ValueError(f"FAQ tối đa {MAX_FAQ_LEN} ký tự.")
    return cleaned_note, cleaned_price, cleaned_faq


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
        raise ValueError("UID không hợp lệ (cần 1–20 chữ số hoặc link Facebook cá nhân).")

    raw_note = parts[1] if len(parts) >= 2 else ""
    raw_price = parts[2] if len(parts) >= 3 else ""
    raw_faq = parts[3] if len(parts) >= 4 else ""
    note, price, faq = normalize_watch_fields(raw_note, raw_price, raw_faq)
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


def _format_price(raw_price: str) -> str:
    cleaned = raw_price.strip()
    if not cleaned:
        return "0đ"
    if cleaned.isdigit():
        num = int(cleaned)
        return f"{num:,}đ".replace(",", ".") if num >= 1000 else f"{num}đ"
    return cleaned


def _spoiler(html_text: str, hidden: bool) -> str:
    return f"<tg-spoiler>{html_text}</tg-spoiler>" if hidden else html_text


def format_watch_card(item: WatchItem, title: str | None = None) -> str:
    if title is None:
        header = (
            f"🔔 🟢 <b>THÔNG BÁO TÀI KHOẢN ĐÃ LIVE!</b> <i>(#{item.id})</i>"
            if item.status == WatchStatus.LIVE
            else f"✅ <b>Chi tiết kèo theo dõi</b> <i>(#{item.id})</i>"
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
        uid_line = f"✅ <b>UID</b> {uid_val} - <b>đã Sống lại</b>"
    elif item.status == WatchStatus.DIE:
        uid_line = f"❌ <b>UID</b> {uid_val} - <b>DIE</b>"
    elif item.status == WatchStatus.DONE:
        uid_line = f"✅ <b>UID</b> {uid_val} - <b>DONE</b>"
    else:
        uid_line = f"❌ <b>UID</b> {uid_val} - <b>ĐÃ HỦY</b>"

    lines = [header, "", uid_line]
    if item.fb_name:
        lines.append(
            f"👤 <b>Tên Facebook:</b> {_spoiler(html.escape(item.fb_name), item.is_hidden)}"
        )
    elif item.status == WatchStatus.DIE:
        lines.append(f"👤 <b>Tên Facebook:</b> {_spoiler('UID DIE', item.is_hidden)}")
    lines.append(f"📝 <b>Ghi chú:</b> {note_val}")
    lines.append(f"💰 <b>Giá:</b> {price_val}")

    if item.faq:
        faq_raw = (
            f"{html.escape(item.faq)} → LIVE ✅"
            if item.status == WatchStatus.LIVE
            else html.escape(item.faq)
        )
        lines.append(f"⚠️ <b>FAQ:</b> {_spoiler(faq_raw, item.is_hidden)}")

    progress_line = {
        WatchStatus.DIE: "<b>Tiến trình:</b> Đang theo dõi chờ LIVE 🔴",
        WatchStatus.LIVE: "<b>Tiến trình:</b> Đã LIVE lại 🟢",
        WatchStatus.DONE: "<b>Tiến trình:</b> Đã Done kèo ✅",
        WatchStatus.CANCELLED: "<b>Tiến trình:</b> Đã Hủy kèo ❌",
    }[item.status]
    lines.append(progress_line)

    aware_time = (
        item.created_at
        if item.created_at.tzinfo is not None
        else item.created_at.replace(tzinfo=UTC)
    )
    vn_time = aware_time.astimezone(timezone(timedelta(hours=7))).strftime("%H:%M:%S %d/%m/%Y")
    lines.append(f"🕒 <b>Thời gian khởi tạo:</b> {_spoiler(vn_time, item.is_hidden)}")

    if item.live_detected_at:
        elapsed = format_duration(item.created_at, item.live_detected_at)
        lines.append(f"└ <b>Thời gian xử lý:</b> {_spoiler(elapsed, item.is_hidden)}")

    return "\n".join(lines)


def format_live_notification(item: WatchItem) -> str:
    return format_watch_card(item, title="🔔 🟢 THÔNG BÁO TÀI KHOẢN ĐÃ LIVE!")


def _format_list_note(note: str, max_len: int = 120) -> str:
    cleaned = " ".join(note.split())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    # Word Joiner (\u2060) prevents Telegram from auto-linking emails/domains AND forbids mid-email line breaks
    return html.escape(cleaned).replace("@", "@\u2060").replace(".", ".\u2060")


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
        f"📋 <b>Danh sách kèo</b> · <b>{html.escape(status_filter)}</b> "
        f"(Trang {page}/{total_pages} · {total_items} kèo)"
    )
    if not items:
        return f"{header}\n\n📭 Chưa có kèo nào phù hợp."
    blocks: list[str] = [header]
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
        price_display = html.escape(_format_price(item.price))
        faq_part = f"  [{html.escape(item.faq)}]" if item.faq else ""
        note_display = _format_list_note(item.note)
        blocks.append(
            f"{badge} <b>#\u200b{item.id}</b> · {uid_display} · <b>{price_display}</b>{faq_part}\n"
            f"   └ {note_display}"
        )
    hint = (
        "<i>⇨ Chạm UID để mở chi tiết · hoặc gửi UID / link FB để tra cứu.</i>"
        if bot_username
        else "<i>⇨ Gửi UID hoặc link Facebook để tra cứu nhanh chi tiết kèo.</i>"
    )
    blocks.append(hint)
    return "\n\n".join(blocks)


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
        "• Chọn 1 - 4 nút ở dưới: <b>Lên kèo</b>, <b>Danh sách</b>, <b>Check nhanh</b>, <b>Thống kê</b>\n"
        "• Hoặc dán thẳng <b>UID / link Facebook / nhiều dòng UID / file .txt</b>"
    )


def format_watch_stats(stats: WatchStats) -> str:
    return (
        "📊 <b>Thống kê kèo hiện có</b>\n\n"
        f"• Tổng số kèo: <b>{stats.total}</b>\n"
        f"• 🔴 Đang theo dõi (DIE): <b>{stats.die}</b>\n"
        f"• 🟢 Đã về LIVE: <b>{stats.live}</b>\n"
        f"• ✅ Đã Done: <b>{stats.done}</b>\n"
        f"• ❌ Đã Hủy: <b>{stats.cancelled}</b>"
    )
