from __future__ import annotations

import html

from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from pydantic import BaseModel

from src.modules.watch.domain import WatchItem, WatchStatus


class WizardStates(StatesGroup):
    waiting_uid = State()
    waiting_note = State()
    waiting_price = State()
    waiting_faq = State()


class ListLookupStates(StatesGroup):
    waiting_uid = State()


class WatchActionCallback(CallbackData, prefix="w"):
    action: str
    watch_id: int


class QuickWatchCallback(CallbackData, prefix="qw"):
    uid: str


class ListPageCallback(CallbackData, prefix="lp"):
    status: str
    page: int


class WizardCallback(CallbackData, prefix="wz"):
    action: str
    value: str = ""


class DealDraft(BaseModel):
    uid: str = ""
    note: str = ""
    price: str = ""
    faq: str = ""
    wizard_message_id: int | None = None
    edit_watch_id: int | None = None


def build_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Lên kèo"), KeyboardButton(text="📋 Danh sách")],
            [KeyboardButton(text="🔎 Check nhanh"), KeyboardButton(text="📊 Thống kê")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_wizard_keyboard(step: int, *, min_step: int = 1) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if step == 3:
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=WizardCallback(action="pick_price", value=val).pack(),
                )
                for label, val in (
                    ("0đ", "0"),
                    ("50k", "50k"),
                    ("100k", "100k"),
                    ("200k", "200k"),
                    ("500k", "500k"),
                )
            ]
        )
    elif step == 4:
        rows.append(
            [
                InlineKeyboardButton(
                    text="282",
                    callback_data=WizardCallback(action="pick_faq", value="282").pack(),
                ),
                InlineKeyboardButton(
                    text="956",
                    callback_data=WizardCallback(action="pick_faq", value="956").pack(),
                ),
                InlineKeyboardButton(
                    text="583",
                    callback_data=WizardCallback(action="pick_faq", value="583").pack(),
                ),
                InlineKeyboardButton(
                    text="⏭ Bỏ qua",
                    callback_data=WizardCallback(action="skip_faq").pack(),
                ),
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if step > min_step:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Quay lại",
                callback_data=WizardCallback(action="prev", value=str(step - 1)).pack(),
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text="❌ Hủy",
            callback_data=WizardCallback(action="cancel").pack(),
        )
    )
    rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_wizard_step(step: int, draft: DealDraft) -> str:
    summary: list[str] = []
    if draft.uid:
        summary.append(f"🆔 UID: <code>{html.escape(draft.uid)}</code>")
    if draft.note:
        summary.append(f"📝 Ghi chú: {html.escape(draft.note)}")
    if draft.price:
        summary.append(f"💰 Giá: {html.escape(draft.price)}")

    if draft.edit_watch_id is not None:
        header = f"✏️ <b>SỬA KÈO #{draft.edit_watch_id}</b>"
        prompts = {
            2: "📝 <b>Bước 1/3 — Nhập ghi chú mới</b> cho kèo (VD: <code>Kèo 1</code>):",
            3: "💰 <b>Bước 2/3 — Chọn nhanh giá mới bên dưới</b> hoặc gõ giá bất kỳ:",
            4: "❓ <b>Bước 3/3 — Chọn nhanh FAQ mới bên dưới</b> hoặc gõ lưu ý bất kỳ (gõ <code>-</code> để bỏ trống):",
        }
    else:
        header = "📌 <b>LÊN KÈO NHANH</b>"
        prompts = {
            1: "🆔 <b>Bước 1/4 — Nhập UID</b> (hoặc dán link Facebook):",
            2: "📝 <b>Bước 2/4 — Nhập ghi chú</b> cho kèo (VD: <code>Kèo 1</code>):",
            3: "💰 <b>Bước 3/4 — Chọn nhanh giá bên dưới</b> hoặc gõ giá bất kỳ:",
            4: "❓ <b>Bước 4/4 — Chọn nhanh FAQ bên dưới</b> hoặc gõ lưu ý bất kỳ (gõ <code>-</code> để bỏ trống):",
        }
    body = ("\n" + "\n".join(summary) + "\n") if summary else ""
    return f"{header}\n{body}\n👉 {prompts[step]}"


def build_watch_keyboard(
    item: WatchItem,
    *,
    back_status: str | None = "ALL",
    back_page: int = 1,
) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton(
            text="🐵 Hiện thông tin",
            callback_data=WatchActionCallback(action="show", watch_id=item.id).pack(),
        )
        if item.is_hidden
        else InlineKeyboardButton(
            text="🙈 Ẩn thông tin",
            callback_data=WatchActionCallback(action="hide", watch_id=item.id).pack(),
        )
    )
    rows: list[list[InlineKeyboardButton]] = [[toggle_btn]]

    def _btn(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text,
            callback_data=WatchActionCallback(action=action, watch_id=item.id).pack(),
        )

    back_btn = (
        InlineKeyboardButton(
            text="🔙 Về danh sách",
            callback_data=ListPageCallback(status=back_status, page=back_page).pack(),
        )
        if back_status is not None
        else None
    )

    if item.status == WatchStatus.DIE:
        rows.append([_btn("❌ Hủy kèo", "cancel"), _btn("🗑 Xóa", "delete")])
        footer: list[InlineKeyboardButton] = [_btn("✏️ Sửa", "edit")]
    elif item.status == WatchStatus.LIVE:
        rows.append([_btn("❌ Hủy kèo", "cancel"), _btn("✅ Done kèo", "done")])
        rows.append([_btn("✏️ Sửa", "edit"), _btn("🗑 Xóa", "delete")])
        footer = [_btn("🔄 Tiếp tục theo dõi", "continue")]
    elif item.status == WatchStatus.DONE:
        rows.append([_btn("🔄 Tiếp tục theo dõi", "continue"), _btn("✏️ Sửa", "edit")])
        rows.append([_btn("❌ Hủy kèo", "cancel"), _btn("🗑 Xóa", "delete")])
        footer = []
    else:  # CANCELLED
        rows.append([_btn("🔄 Tiếp tục theo dõi", "continue"), _btn("✏️ Sửa", "edit")])
        footer = [_btn("🗑 Xóa", "delete")]

    if back_btn is not None:
        footer.append(back_btn)
    if footer:
        rows.append(footer)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_delete_confirm_keyboard(watch_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⚠️ Xác nhận xóa #{watch_id}",
                    callback_data=WatchActionCallback(
                        action="delete_confirm",
                        watch_id=watch_id,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="↩️ Không xóa",
                    callback_data=WatchActionCallback(
                        action="delete_cancel",
                        watch_id=watch_id,
                    ).pack(),
                ),
            ]
        ]
    )


def build_list_keyboard(
    status_filter: str,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    filters = [
        ("ALL", "ALL"),
        ("DIE", "DIE"),
        ("LIVE", "LIVE"),
        ("DONE", "DONE"),
        ("CANCELLED", "HỦY"),
    ]
    filter_row = [
        InlineKeyboardButton(
            text=f"•{label}•" if code == status_filter else label,
            callback_data=ListPageCallback(status=code, page=1).pack(),
        )
        for code, label in filters
    ]
    rows: list[list[InlineKeyboardButton]] = [filter_row]
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=ListPageCallback(status=status_filter, page=page - 1).pack(),
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"📄 {page}/{total_pages}",
                callback_data=ListPageCallback(status=status_filter, page=page).pack(),
            )
        )
        if page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=ListPageCallback(status=status_filter, page=page + 1).pack(),
                )
            )
        rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)
