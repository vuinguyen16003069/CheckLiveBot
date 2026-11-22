from __future__ import annotations

import html
import re
from collections.abc import Sequence

from pydantic import BaseModel

from src.core.ports import CheckOutcome

_UID_RE = re.compile(r"^\d{5,20}$")
_FB_PROFILE_ID_RE = re.compile(r"(?:profile\.php\?[^#\s]*\bid=)(\d{5,20})", re.IGNORECASE)
_ANY_UID_TOKEN_RE = re.compile(r"\b(\d{5,20})\b")


class BulkSummary(BaseModel):
    job_id: str
    total: int
    live: int
    die: int
    error: int
    cache_hits: int
    elapsed_seconds: float


def extract_uid(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if _UID_RE.match(text):
        return text
    match = _FB_PROFILE_ID_RE.search(text)
    if match:
        return match.group(1)
    return None


def extract_uids_from_text(text: str, max_uids: int = 50000) -> tuple[list[str], int]:
    """Extract deduplicated UIDs preserving order. Returns (uids, duplicate_or_skipped_count)."""
    seen: set[str] = set()
    uids: list[str] = []
    skipped = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        uid = extract_uid(stripped)
        if uid is None:
            token_match = _ANY_UID_TOKEN_RE.search(stripped)
            uid = token_match.group(1) if token_match else None
        if uid is None:
            skipped += 1
            continue
        if uid in seen:
            skipped += 1
            continue
        seen.add(uid)
        uids.append(uid)
        if len(uids) >= max_uids:
            break
    return uids, skipped


def render_progress_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "[" + "░" * width + "] 0%"
    ratio = min(max(done / total, 0.0), 1.0)
    filled = round(ratio * width)
    percent = int(ratio * 100)
    return f"[{'█' * filled}{'░' * (width - filled)}] {percent}%"


def format_bulk_progress(
    job_id: str,
    done: int,
    total: int,
    live: int,
    die: int,
    error: int,
    cache_hits: int,
) -> str:
    bar = render_progress_bar(done, total)
    return (
        f"⚙️ <b>Đang kiểm tra hàng loạt</b> (<code>#{html.escape(job_id)}</code>)\n\n"
        f"<code>{bar}</code> ({done:,}/{total:,})\n"
        f"✅ LIVE: <b>{live:,}</b> | ❌ DIE: <b>{die:,}</b> | ⚠️ Lỗi: <b>{error:,}</b>\n"
        f"⚡ Cache RAM: <b>{cache_hits:,}</b>"
    )


def format_bulk_summary(summary: BulkSummary) -> str:
    return (
        f"🏁 <b>Hoàn tất kiểm tra hàng loạt</b> (<code>#{html.escape(summary.job_id)}</code>)\n\n"
        f"• Tổng UID: <b>{summary.total:,}</b>\n"
        f"• ✅ LIVE: <b>{summary.live:,}</b>\n"
        f"• ❌ DIE: <b>{summary.die:,}</b>\n"
        f"• ⚠️ ERROR: <b>{summary.error:,}</b>\n"
        f"• ⚡ Cache RAM hit: <b>{summary.cache_hits:,}</b>\n"
        f"• ⏱ Thời gian: <b>{summary.elapsed_seconds:.1f}s</b>"
    )


def format_single_check(uid: str, outcome: CheckOutcome, from_cache: bool) -> str:
    badge = {
        CheckOutcome.LIVE: "✅ <b>LIVE</b>",
        CheckOutcome.DIE: "❌ <b>DIE</b>",
        CheckOutcome.ERROR: "⚠️ <b>LỖI MẠNG / KHÔNG XÁC ĐỊNH</b>",
    }[outcome]
    source = " (⚡ Cache RAM)" if from_cache else ""
    return f"🔎 <b>Kiểm tra nhanh UID</b>\n🆔 <code>{html.escape(uid)}</code>\n📌 Trạng thái: {badge}{source}"


def build_result_files(
    live_uids: Sequence[str],
    die_uids: Sequence[str],
    error_uids: Sequence[str],
) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    if live_uids:
        files.append(("live.txt", ("\n".join(live_uids) + "\n").encode("utf-8")))
    if die_uids:
        files.append(("die.txt", ("\n".join(die_uids) + "\n").encode("utf-8")))
    if error_uids:
        files.append(("error.txt", ("\n".join(error_uids) + "\n").encode("utf-8")))
    return files
