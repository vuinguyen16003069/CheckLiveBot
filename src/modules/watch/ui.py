from __future__ import annotations

import html

from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import BaseModel

from src.modules.watch.domain import WatchItem, WatchStatus


class WizardStates(StatesGroup):
    waiting_uid = State()
    waiting_note = State()
    waiting_price = State()
    waiting_faq = State()


class DealFormStates(StatesGroup):
    browsing = State()
    waiting_for_field = State()


class AddListStates(StatesGroup):
    waiting_for_lines = State()


class SearchStates(StatesGroup):
    waiting_for_query = State()


class CheckOneStates(StatesGroup):
    waiting_for_uid = State()


class MenuCallback(CallbackData, prefix="m"):
    action: str


class WatchActionCallback(CallbackData, prefix="w"):
    action: str
    watch_id: int


class QuickWatchCallback(CallbackData, prefix="qw"):
    uid: str


class ListPageCallback(CallbackData, prefix="lp"):
    status: str
    page: int


class DealFormCallback(CallbackData, prefix="df"):
    action: str
    field: str = ""


class WizardCallback(CallbackData, prefix="wz"):
    action: str


class DealDraft(BaseModel):
    watch_id: int | None = None
    uid: str = ""
    note: str = ""
    price: str = ""
    faq: str = ""
    form_message_id: int | None = None
    editing_field: str | None = None

    @property
    def is_edit(self) -> bool:
        return self.watch_id is not None


FIELD_PROMPTS: dict[str, str] = {
    "uid": (
        "🆔 Vui lòng gửi <b>UID</b> hoặc <b>link Facebook</b> "
        "(VD: <code>100012345678901</code> hoặc <code>https://facebook.com/profile.php?id=...</code>):"
    ),
    "note": "📝 Vui lòng gửi <b>ghi chú</b> cho kèo (VD: <code>Khách VIP A</code>):",
    "price": "💰 Vui lòng gửi <b>giá kèo</b> (VD: <code>50k</code>, <code>150.000</code>):",
    "faq": "❓ Vui lòng gửi <b>FAQ / lưu ý thêm</b> (gửi <code>-</code> để xóa trắng):",
}


def format_deal_form(draft: DealDraft) -> str:
    header = (
        f"✏️ <b>SỬA KÈO #{draft.watch_id}</b> (Bấm từng nút bên dưới để sửa)\n"
        if draft.is_edit
        else "📝 <b>LÊN KÈO MỚI</b> (Bấm từng nút bên dưới để nhập)\n"
    )
    uid_val = (
        f"<code>{html.escape(draft.uid)}</code> ✅"
        if draft.uid
        else "<i>(chưa nhập — bắt buộc)</i>"
    )
    note_val = (
        f"{html.escape(draft.note)} ✅"
        if draft.note
        else "<i>(chưa nhập — mặc định: Theo dõi nhanh)</i>"
    )
    price_val = (
        f"{html.escape(draft.price)} ✅" if draft.price else "<i>(chưa nhập — mặc định: 0)</i>"
    )
    faq_val = f"{html.escape(draft.faq)} ✅" if draft.faq else "<i>(tùy chọn)</i>"
    return (
        f"{header}\n"
        f"🆔 UID: {uid_val}\n"
        f"📝 Ghi chú: {note_val}\n"
        f"💰 Giá: {price_val}\n"
        f"❓ FAQ: {faq_val}"
    )


def build_deal_form_keyboard(draft: DealDraft) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if draft.is_edit:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📝 Sửa ghi chú",
                    callback_data=DealFormCallback(action="field", field="note").pack(),
                ),
                InlineKeyboardButton(
                    text="💰 Sửa giá",
                    callback_data=DealFormCallback(action="field", field="price").pack(),
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="❓ Sửa FAQ",
                    callback_data=DealFormCallback(action="field", field="faq").pack(),
                )
            ]
        )
        confirm_text = "💾 Lưu thay đổi"
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🆔 Nhập UID",
                    callback_data=DealFormCallback(action="field", field="uid").pack(),
                ),
                InlineKeyboardButton(
                    text="📝 Nhập ghi chú",
                    callback_data=DealFormCallback(action="field", field="note").pack(),
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="💰 Nhập giá",
                    callback_data=DealFormCallback(action="field", field="price").pack(),
                ),
                InlineKeyboardButton(
                    text="❓ Nhập FAQ",
                    callback_data=DealFormCallback(action="field", field="faq").pack(),
                ),
            ]
        )
        confirm_text = "✅ Xác nhận lên kèo"

    rows.append(
        [
            InlineKeyboardButton(
                text=confirm_text,
                callback_data=DealFormCallback(action="save").pack(),
            ),
            InlineKeyboardButton(
                text="❌ Hủy",
                callback_data=DealFormCallback(action="cancel").pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_field_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Quay lại Form",
                    callback_data=DealFormCallback(action="back").pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Hủy",
                    callback_data=DealFormCallback(action="cancel").pack(),
                ),
            ]
        ]
    )


def build_wizard_keyboard(allow_skip: bool = False) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if allow_skip:
        row.append(
            InlineKeyboardButton(
                text="⏭ Bỏ qua & Lên kèo",
                callback_data=WizardCallback(action="skip_faq").pack(),
            )
        )
    row.append(
        InlineKeyboardButton(
            text="❌ Hủy",
            callback_data=WizardCallback(action="cancel").pack(),
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def format_wizard_step(step: int, draft: DealDraft) -> str:
    summary: list[str] = []
    if draft.uid:
        summary.append(f"🆔 UID: <code>{html.escape(draft.uid)}</code>")
    if draft.note:
        summary.append(f"📝 Ghi chú: {html.escape(draft.note)}")
    if draft.price:
        summary.append(f"💰 Giá: {html.escape(draft.price)}")

    prompts = {
        1: "🆔 <b>Bước 1/4 — Nhập UID</b> (hoặc link <code>profile.php?id=...</code>):",
        2: "📝 <b>Bước 2/4 — Nhập ghi chú</b> cho kèo (VD: <code>Khách VIP A</code>):",
        3: "💰 <b>Bước 3/4 — Nhập giá kèo</b> (VD: <code>50k</code>, <code>150.000</code>):",
        4: "❓ <b>Bước 4/4 — Nhập FAQ / lưu ý thêm</b> (hoặc bấm <b>Bỏ qua &amp; Lên kèo</b>):",
    }
    header = "📌 <b>LÊN KÈO TỪNG BƯỚC</b>"
    body = ("\n" + "\n".join(summary) + "\n") if summary else ""
    return f"{header}\n{body}\n👉 {prompts[step]}"


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Hủy",
                    callback_data=MenuCallback(action="cancel_fsm").pack(),
                ),
                InlineKeyboardButton(
                    text="🔙 Menu chính",
                    callback_data=MenuCallback(action="home").pack(),
                ),
            ]
        ]
    )


def build_back_menu_keyboard(refresh_action: str | None = None) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if refresh_action:
        row.append(
            InlineKeyboardButton(
                text="🔄 Làm mới",
                callback_data=MenuCallback(action=refresh_action).pack(),
            )
        )
    row.append(
        InlineKeyboardButton(
            text="🔙 Quay lại Menu",
            callback_data=MenuCallback(action="home").pack(),
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Lên kèo",
                    callback_data=MenuCallback(action="add").pack(),
                ),
                InlineKeyboardButton(
                    text="📥 Thêm nhiều kèo",
                    callback_data=MenuCallback(action="addlist").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔎 Check 1 UID",
                    callback_data=MenuCallback(action="check_one").pack(),
                ),
                InlineKeyboardButton(
                    text="📦 Check hàng loạt",
                    callback_data=MenuCallback(action="bulk").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Danh sách kèo",
                    callback_data=ListPageCallback(status="ALL", page=1).pack(),
                ),
                InlineKeyboardButton(
                    text="🔍 Tìm kèo",
                    callback_data=MenuCallback(action="find").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Thống kê",
                    callback_data=MenuCallback(action="stats").pack(),
                ),
                InlineKeyboardButton(
                    text="📜 Lịch sử",
                    callback_data=MenuCallback(action="history").pack(),
                ),
            ],
        ]
    )


def build_watch_keyboard(item: WatchItem) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🙈 Ẩn thông tin",
                callback_data=WatchActionCallback(action="hide", watch_id=item.id).pack(),
            ),
            InlineKeyboardButton(
                text="🐵 Hiện thông tin",
                callback_data=WatchActionCallback(action="show", watch_id=item.id).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="❌ Hủy kèo",
                callback_data=WatchActionCallback(action="cancel", watch_id=item.id).pack(),
            ),
            InlineKeyboardButton(
                text="✅ Done kèo",
                callback_data=WatchActionCallback(action="done", watch_id=item.id).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔄 Kiểm tra lại"
                if item.status == WatchStatus.DIE
                else "✅ Tiếp tục theo dõi",
                callback_data=WatchActionCallback(
                    action="recheck" if item.status == WatchStatus.DIE else "continue",
                    watch_id=item.id,
                ).pack(),
            ),
            InlineKeyboardButton(
                text="✏️ Sửa kèo",
                callback_data=WatchActionCallback(action="edit", watch_id=item.id).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="🗑 Xóa kèo",
                callback_data=WatchActionCallback(action="delete", watch_id=item.id).pack(),
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_list_keyboard(status_filter: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    filters = ["ALL", "DIE", "LIVE", "DONE", "CANCELLED"]
    filter_row = [
        InlineKeyboardButton(
            text=f"• {st} •" if st == status_filter else st,
            callback_data=ListPageCallback(status=st, page=1).pack(),
        )
        for st in filters
    ]
    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text="◀️ Trước",
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
                text="Sau ▶️",
                callback_data=ListPageCallback(status=status_filter, page=page + 1).pack(),
            )
        )
    rows = [filter_row[:3], filter_row[3:], nav_row]
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Quay lại Menu",
                callback_data=MenuCallback(action="home").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
