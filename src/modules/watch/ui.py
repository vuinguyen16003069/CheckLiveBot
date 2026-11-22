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

REPLY_BTN_ADD = "➕ Lên kèo"
REPLY_BTN_LIST = "📋 Danh sách"
REPLY_BTN_CHECK = "🔎 Check nhanh"
REPLY_BTN_STATS = "📊 Thống kê"
REPLY_SHORTCUTS = {REPLY_BTN_ADD, REPLY_BTN_LIST, REPLY_BTN_CHECK, REPLY_BTN_STATS}


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


class OpenWatchCallback(CallbackData, prefix="ow"):
    watch_id: int
    back_status: str = "ALL"
    back_page: int = 1


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
    value: str = ""


class DealDraft(BaseModel):
    watch_id: int | None = None
    uid: str = ""
    note: str = ""
    price: str = ""
    faq: str = ""
    form_message_id: int | None = None
    wizard_message_id: int | None = None
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


def build_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=REPLY_BTN_ADD), KeyboardButton(text=REPLY_BTN_LIST)],
            [KeyboardButton(text=REPLY_BTN_CHECK), KeyboardButton(text=REPLY_BTN_STATS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


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
                    text="🆔 Nhập UID *",
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


def build_wizard_keyboard(step: int) -> InlineKeyboardMarkup:
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
                    text="⏭ Bỏ qua",
                    callback_data=WizardCallback(action="skip_faq").pack(),
                ),
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if step > 1:
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

    prompts = {
        1: "🆔 <b>Bước 1/4 — Nhập UID</b> (hoặc dán link <code>profile.php?id=...</code>):",
        2: "📝 <b>Bước 2/4 — Nhập ghi chú</b> cho kèo (VD: <code>Kèo 1</code>):",
        3: "💰 <b>Bước 3/4 — Chọn nhanh giá bên dưới</b> hoặc gõ giá bất kỳ:",
        4: "❓ <b>Bước 4/4 — Chọn nhanh FAQ bên dưới</b>, gõ lưu ý, hoặc bấm <b>Bỏ qua</b>:",
    }
    header = "📌 <b>LÊN KÈO NHANH</b>"
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


def build_watch_keyboard(
    item: WatchItem,
    *,
    back_status: str | None = None,
    back_page: int = 1,
) -> InlineKeyboardMarkup:
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
    ]
    if item.status == WatchStatus.DIE:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Kiểm tra lại",
                    callback_data=WatchActionCallback(action="recheck", watch_id=item.id).pack(),
                ),
                InlineKeyboardButton(
                    text="✏️ Sửa kèo",
                    callback_data=WatchActionCallback(action="edit", watch_id=item.id).pack(),
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Hủy kèo",
                    callback_data=WatchActionCallback(action="cancel", watch_id=item.id).pack(),
                ),
                InlineKeyboardButton(
                    text="🗑 Xóa",
                    callback_data=WatchActionCallback(action="delete", watch_id=item.id).pack(),
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Hủy kèo",
                    callback_data=WatchActionCallback(action="cancel", watch_id=item.id).pack(),
                ),
                InlineKeyboardButton(
                    text="✅ Done kèo",
                    callback_data=WatchActionCallback(action="done", watch_id=item.id).pack(),
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Tiếp tục theo dõi",
                    callback_data=WatchActionCallback(action="continue", watch_id=item.id).pack(),
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="✏️ Sửa kèo",
                    callback_data=WatchActionCallback(action="edit", watch_id=item.id).pack(),
                ),
                InlineKeyboardButton(
                    text="🗑 Xóa",
                    callback_data=WatchActionCallback(action="delete", watch_id=item.id).pack(),
                ),
            ]
        )

    if back_status is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔙 Về danh sách",
                    callback_data=ListPageCallback(status=back_status, page=back_page).pack(),
                )
            ]
        )
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


def _build_item_picker_rows(
    items: list[WatchItem],
    *,
    back_status: str = "ALL",
    back_page: int = 1,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        InlineKeyboardButton(
            text=f"#{item.id}",
            callback_data=OpenWatchCallback(
                watch_id=item.id,
                back_status=back_status,
                back_page=back_page,
            ).pack(),
        )
        for item in items[:8]
    ]
    for idx in range(0, len(buttons), 4):
        rows.append(buttons[idx : idx + 4])
    return rows


def build_list_keyboard(
    status_filter: str,
    page: int,
    total_pages: int,
    items: list[WatchItem] | None = None,
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
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Quét lại DIE",
                callback_data=MenuCallback(action="recheck_all").pack(),
            ),
            InlineKeyboardButton(
                text="🔙 Menu",
                callback_data=MenuCallback(action="home").pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_search_results_keyboard(items: list[WatchItem]) -> InlineKeyboardMarkup:
    rows = _build_item_picker_rows(items, back_status="ALL", back_page=1)
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Quay lại Menu",
                callback_data=MenuCallback(action="home").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
