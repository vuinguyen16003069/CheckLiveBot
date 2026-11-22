from __future__ import annotations

import contextlib
import io

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.core.ports import CheckOutcome
from src.modules.bulk.domain import extract_uid
from src.modules.watch.domain import (
    WatchItem,
    WatchStatus,
    format_live_notification,
    format_search_results,
    format_watch_card,
    format_watch_history,
    format_watch_list,
    format_watch_stats,
)
from src.modules.watch.service import WatchService


class AddOneStates(StatesGroup):
    waiting_for_item = State()


class AddListStates(StatesGroup):
    waiting_for_lines = State()


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


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Thêm kèo",
                    callback_data=MenuCallback(action="add").pack(),
                ),
                InlineKeyboardButton(
                    text="📥 Thêm nhiều kèo",
                    callback_data=MenuCallback(action="addlist").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📦 Check hàng loạt",
                    callback_data=MenuCallback(action="bulk").pack(),
                ),
                InlineKeyboardButton(
                    text="📋 Danh sách kèo",
                    callback_data=ListPageCallback(status="ALL", page=1).pack(),
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
            [
                InlineKeyboardButton(
                    text="❓ Hướng dẫn",
                    callback_data=MenuCallback(action="help").pack(),
                )
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
    ]
    if item.status == WatchStatus.DIE:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Kiểm tra lại",
                    callback_data=WatchActionCallback(action="recheck", watch_id=item.id).pack(),
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
                    text="✅ Tiếp tục theo dõi",
                    callback_data=WatchActionCallback(action="continue", watch_id=item.id).pack(),
                ),
                InlineKeyboardButton(
                    text="🗑 Xóa",
                    callback_data=WatchActionCallback(action="delete", watch_id=item.id).pack(),
                ),
            ]
        )
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


WELCOME_TEXT = (
    "👋 <b>Chào mừng đến với CheckLiveBot!</b>\n\n"
    "✨ <b>Dùng cực nhanh không cần nhớ lệnh:</b>\n"
    "• Dán thẳng <b>1 UID</b> hoặc <b>link Facebook</b> → Bot tự check ngay\n"
    "• Dán <b>nhiều dòng UID</b> hoặc gửi file <code>.txt</code> → Tự check hàng loạt\n\n"
    "Chọn nhanh tính năng bên dưới hoặc gõ /help:"
)

HELP_TEXT = (
    "📖 <b>Hướng dẫn sử dụng CheckLiveBot</b>\n\n"
    "<b>1. Dùng nhanh không cần gõ lệnh</b>\n"
    "• Dán <code>100012345678901</code> hoặc link <code>profile.php?id=...</code> → Check ngay\n"
    "• Dán danh sách nhiều dòng UID hoặc gửi file <code>.txt</code> → Check hàng loạt (ARQ Worker)\n\n"
    "<b>2. Theo dõi kèo DIE → LIVE</b>\n"
    "• <code>/add UID | ghi chú | giá [| FAQ]</code> *(hoặc gọn: <code>/add UID</code>)*\n"
    "• <code>/addlist</code> — Thêm nhiều kèo cùng lúc\n"
    "• <code>/list</code> — Danh sách kèo (lọc & phân trang)\n"
    "• <code>/find &lt;UID / từ khóa&gt;</code> — Tìm nhanh kèo\n"
    "• <code>/recheck [UID]</code> — Quét lại ngay kèo đang DIE\n"
    "• <code>/stats</code> | <code>/history</code> — Thống kê & lịch sử"
)


class TelegramWatchNotifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def notify_live(self, item: WatchItem) -> None:
        await self._bot.send_message(
            chat_id=item.chat_id,
            text=format_live_notification(item),
            reply_markup=build_watch_keyboard(item),
        )


def create_watch_router(watch_service: WatchService) -> Router:
    router = Router(name="watch")

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(WELCOME_TEXT, reply_markup=build_main_menu_keyboard())

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT, reply_markup=build_main_menu_keyboard())

    @router.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject, state: FSMContext) -> None:
        raw = (command.args or "").strip()
        if not raw:
            await state.set_state(AddOneStates.waiting_for_item)
            await message.answer(
                "📝 <b>Nhập thông tin kèo cần theo dõi</b> (không cần gõ lại /add):\n"
                "• Đầy đủ: <code>100012345678901 | Khách A | 50k | FAQ</code>\n"
                "• Gọn: <code>100012345678901 Khách A | 50k</code> hoặc chỉ <code>100012345678901</code>\n\n"
                "Gõ /cancel để hủy."
            )
            return
        await _handle_upsert_watch(message, watch_service, raw)

    @router.message(AddOneStates.waiting_for_item, F.text)
    async def handle_add_one_text(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if text.lower() == "/cancel":
            await state.clear()
            await message.answer("🛑 Đã hủy thêm kèo.")
            return
        await state.clear()
        await _handle_upsert_watch(message, watch_service, text)

    @router.message(Command("addlist"))
    async def cmd_addlist(message: Message, state: FSMContext) -> None:
        await state.set_state(AddListStates.waiting_for_lines)
        await message.answer(
            "📥 <b>Thêm nhiều kèo cùng lúc</b>\n\n"
            "Gửi danh sách (mỗi dòng 1 kèo) hoặc file <code>.txt</code> theo mẫu:\n"
            "<code>UID | ghi chú | giá [| FAQ]</code>\n\n"
            "Gõ /cancel để hủy."
        )

    @router.message(AddListStates.waiting_for_lines, F.text)
    async def handle_addlist_text(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if text.lower() == "/cancel":
            await state.clear()
            await message.answer("🛑 Đã hủy thêm nhiều kèo.")
            return
        await state.clear()
        user_id = message.from_user.id if message.from_user else 0
        added, errors = await watch_service.add_watch_batch(message.chat.id, user_id, text)
        lines = [f"✅ Đã thêm/cập nhật <b>{len(added)}</b> kèo thành công."]
        if errors:
            lines.append(f"⚠️ Có <b>{len(errors)}</b> dòng lỗi:")
            lines.extend(errors[:10])
        await message.answer("\n".join(lines))

    @router.message(AddListStates.waiting_for_lines, F.document)
    async def handle_addlist_doc(message: Message, bot: Bot, state: FSMContext) -> None:
        document = message.document
        if document is None:
            return
        await state.clear()
        buf = io.BytesIO()
        await bot.download(document, destination=buf)
        text = buf.getvalue().decode("utf-8", errors="ignore")
        user_id = message.from_user.id if message.from_user else 0
        added, errors = await watch_service.add_watch_batch(message.chat.id, user_id, text)
        lines = [f"✅ Đã thêm/cập nhật <b>{len(added)}</b> kèo từ file."]
        if errors:
            lines.append(f"⚠️ Có <b>{len(errors)}</b> dòng lỗi:")
            lines.extend(errors[:10])
        await message.answer("\n".join(lines))

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        items, total, total_pages = await watch_service.list_watches(message.chat.id, page=1)
        text = format_watch_list(
            items,
            status_filter="ALL",
            page=1,
            total_pages=total_pages,
            total_items=total,
        )
        await message.answer(text, reply_markup=build_list_keyboard("ALL", 1, total_pages))

    @router.message(Command("find"))
    async def cmd_find(message: Message, command: CommandObject) -> None:
        query = (command.args or "").strip()
        if not query:
            await message.answer("🔍 Cú pháp: <code>/find &lt;UID hoặc từ khóa ghi chú&gt;</code>")
            return
        items = await watch_service.search_watches(message.chat.id, query)
        await message.answer(
            format_search_results(query, items),
            reply_markup=build_main_menu_keyboard(),
        )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        stats = await watch_service.get_stats(message.chat.id)
        await message.answer(format_watch_stats(stats), reply_markup=build_main_menu_keyboard())

    @router.message(Command("history"))
    async def cmd_history(message: Message) -> None:
        events = await watch_service.get_history(message.chat.id, limit=10)
        await message.answer(format_watch_history(events), reply_markup=build_main_menu_keyboard())

    @router.message(Command("recheck"))
    async def cmd_recheck(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        target_uid = extract_uid(arg) if arg else None
        if arg and not target_uid:
            await message.answer("❌ UID không hợp lệ.")
            return
        scanned, turned_live = await watch_service.recheck_chat(
            message.chat.id,
            target_uid=target_uid,
        )
        if scanned == 0:
            await message.answer("📭 Không có kèo DIE nào cần kiểm tra lại.")
            return
        await message.answer(
            f"🔄 Đã quét lại <b>{scanned}</b> kèo DIE — Phát hiện <b>{len(turned_live)}</b> kèo về LIVE."
        )
        for item in turned_live:
            await message.answer(
                format_live_notification(item),
                reply_markup=build_watch_keyboard(item),
            )

    @router.callback_query(MenuCallback.filter())
    async def cb_menu(
        query: CallbackQuery,
        callback_data: MenuCallback,
        state: FSMContext,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        action = callback_data.action
        if action == "home":
            await state.clear()
            await _safe_edit(msg, WELCOME_TEXT, build_main_menu_keyboard())
        elif action == "add":
            await state.set_state(AddOneStates.waiting_for_item)
            await msg.answer(
                "📝 Gửi thông tin kèo cần theo dõi:\n"
                "<code>UID | ghi chú | giá [| FAQ]</code> hoặc chỉ <code>UID</code>.\n"
                "Gõ /cancel để hủy."
            )
        elif action == "addlist":
            await state.set_state(AddListStates.waiting_for_lines)
            await msg.answer(
                "📥 Gửi danh sách kèo (mỗi dòng <code>UID | ghi chú | giá [| FAQ]</code>) "
                "hoặc file <code>.txt</code>. Gõ /cancel để hủy."
            )
        elif action == "bulk":
            await msg.answer(
                "📦 <b>Check hàng loạt</b>\n\n"
                "Bạn chỉ cần <b>dán trực tiếp danh sách nhiều dòng UID</b> hoặc <b>gửi file .txt</b> "
                "vào chat này — Bot sẽ tự nhận diện và chạy ngay!"
            )
        elif action == "stats":
            stats = await watch_service.get_stats(msg.chat.id)
            await _safe_edit(msg, format_watch_stats(stats), build_main_menu_keyboard())
        elif action == "history":
            events = await watch_service.get_history(msg.chat.id, limit=10)
            await _safe_edit(msg, format_watch_history(events), build_main_menu_keyboard())
        elif action == "help":
            await _safe_edit(msg, HELP_TEXT, build_main_menu_keyboard())
        await query.answer()

    @router.callback_query(QuickWatchCallback.filter())
    async def cb_quick_watch(query: CallbackQuery, callback_data: QuickWatchCallback) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        user_id = query.from_user.id if query.from_user else 0
        try:
            item, updated = await watch_service.upsert_watch(
                msg.chat.id,
                user_id,
                callback_data.uid,
            )
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        title = "♻️ Đã cập nhật kèo" if updated else "📌 Đã thêm vào theo dõi"
        await msg.answer(
            format_watch_card(item, title=title),
            reply_markup=build_watch_keyboard(item),
        )
        await query.answer("✅ Đã đưa vào danh sách theo dõi!")

    @router.callback_query(ListPageCallback.filter())
    async def cb_list_page(query: CallbackQuery, callback_data: ListPageCallback) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        status_filter = WatchStatus(callback_data.status) if callback_data.status != "ALL" else None
        items, total, total_pages = await watch_service.list_watches(
            msg.chat.id,
            status_filter=status_filter,
            page=callback_data.page,
        )
        text = format_watch_list(
            items,
            status_filter=callback_data.status,
            page=min(callback_data.page, total_pages),
            total_pages=total_pages,
            total_items=total,
        )
        await _safe_edit(
            msg,
            text,
            build_list_keyboard(
                callback_data.status,
                min(callback_data.page, total_pages),
                total_pages,
            ),
        )
        await query.answer()

    @router.callback_query(WatchActionCallback.filter())
    async def cb_watch_action(query: CallbackQuery, callback_data: WatchActionCallback) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        action = callback_data.action
        watch_id = callback_data.watch_id
        try:
            if action == "delete":
                deleted = await watch_service.delete_watch(msg.chat.id, watch_id)
                await _safe_edit(
                    msg,
                    f"🗑 Đã xóa kèo <b>#{deleted.id}</b> (<code>{deleted.uid}</code>).",
                )
                await query.answer("🗑 Đã xóa kèo.")
                return
            if action == "hide":
                item = await watch_service.set_hidden(msg.chat.id, watch_id, True)
                toast = "🙈 Đã ẩn thông tin kèo."
            elif action == "show":
                item = await watch_service.set_hidden(msg.chat.id, watch_id, False)
                toast = "🐵 Đã hiện thông tin kèo."
            elif action == "done":
                item = await watch_service.mark_status(msg.chat.id, watch_id, WatchStatus.DONE)
                toast = "✅ Đã đánh dấu Done kèo."
            elif action == "cancel":
                item = await watch_service.mark_status(msg.chat.id, watch_id, WatchStatus.CANCELLED)
                toast = "❌ Đã hủy kèo."
            elif action == "continue":
                item = await watch_service.mark_status(msg.chat.id, watch_id, WatchStatus.DIE)
                toast = "🔄 Đã tiếp tục theo dõi kèo."
            elif action == "recheck":
                item, outcome = await watch_service.recheck_one(msg.chat.id, watch_id)
                toast = (
                    "🎉 UID đã về LIVE!"
                    if outcome == CheckOutcome.LIVE
                    else (
                        "🔴 UID vẫn DIE."
                        if outcome == CheckOutcome.DIE
                        else "⚠️ Lỗi mạng, thử lại sau."
                    )
                )
            else:
                await query.answer("Hành động không hợp lệ.", show_alert=True)
                return
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        await _safe_edit(msg, format_watch_card(item), build_watch_keyboard(item))
        await query.answer(toast)

    return router


async def _handle_upsert_watch(message: Message, watch_service: WatchService, raw: str) -> None:
    user_id = message.from_user.id if message.from_user else 0
    try:
        item, updated = await watch_service.upsert_watch(message.chat.id, user_id, raw)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    title = "♻️ Đã cập nhật kèo" if updated else "📌 Thông tin kèo"
    await message.answer(
        format_watch_card(item, title=title),
        reply_markup=build_watch_keyboard(item),
    )


async def _safe_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await message.edit_text(text, reply_markup=reply_markup)
