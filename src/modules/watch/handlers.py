from __future__ import annotations

import contextlib
import io

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from src.core.ports import CheckOutcome
from src.modules.bulk.domain import extract_uid, format_single_check
from src.modules.bulk.service import BulkCheckService
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
from src.modules.watch.ui import (
    FIELD_PROMPTS,
    AddListStates,
    CheckOneStates,
    DealDraft,
    DealFormCallback,
    DealFormStates,
    ListPageCallback,
    MenuCallback,
    QuickWatchCallback,
    SearchStates,
    WatchActionCallback,
    WizardCallback,
    WizardStates,
    build_cancel_keyboard,
    build_deal_form_keyboard,
    build_field_prompt_keyboard,
    build_list_keyboard,
    build_main_menu_keyboard,
    build_watch_keyboard,
    build_wizard_keyboard,
    format_deal_form,
    format_wizard_step,
)

WELCOME_TEXT = (
    "👋 <b>Chào mừng đến với CheckLiveBot!</b>\n\n"
    "✨ <b>Dùng cực nhanh bằng nút bấm UI hoặc lệnh:</b>\n"
    "• Bấm <b>➕ Lên kèo</b> để nhập tuần tự <code>UID → Ghi chú → Giá → FAQ</code> (có nút Hủy xuyên suốt)\n"
    "• Hoặc dán thẳng <b>1 UID / link FB</b> / <b>nhiều dòng UID</b> / <b>file .txt</b>\n\n"
    "Chọn tính năng bên dưới hoặc gõ /help:"
)

HELP_TEXT = (
    "📖 <b>Hướng dẫn sử dụng CheckLiveBot</b>\n\n"
    "<b>1. Dùng bằng giao diện UI (bấm nút nào → nhập cái đó)</b>\n"
    "• Gõ /start để mở Menu nút bấm: Lên kèo, Check 1 UID, Check hàng loạt, Tìm kèo, Danh sách...\n\n"
    "<b>2. Dùng nhanh không cần lệnh</b>\n"
    "• Dán <code>100012345678901</code> hoặc link <code>profile.php?id=...</code> → Check ngay\n"
    "• Dán danh sách nhiều dòng UID hoặc gửi file <code>.txt</code> → Check hàng loạt (ARQ Worker)\n\n"
    "<b>3. Lệnh gõ nhanh</b>\n"
    "• <code>/add UID | ghi chú | giá [| FAQ]</code> *(hoặc gõ <code>/add</code> mở Form UI)*\n"
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


def create_watch_router(
    watch_service: WatchService,
    bulk_service: BulkCheckService | None = None,
) -> Router:
    router = Router(name="watch")

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(WELCOME_TEXT, reply_markup=build_main_menu_keyboard())

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT, reply_markup=build_main_menu_keyboard())

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "🛑 Đã hủy thao tác hiện tại.", reply_markup=build_main_menu_keyboard()
        )

    @router.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject, state: FSMContext) -> None:
        raw = (command.args or "").strip()
        if not raw:
            await _start_wizard(message, state, DealDraft(), step=1)
            return
        await _handle_upsert_watch(message, watch_service, raw)

    @router.message(WizardStates.waiting_uid, F.text)
    async def wizard_step_uid(message: Message, state: FSMContext) -> None:
        uid = extract_uid((message.text or "").strip())
        if not uid:
            await message.answer(
                "❌ UID không hợp lệ (cần 5–20 chữ số hoặc link <code>profile.php?id=...</code>).\n"
                "👉 Vui lòng nhập lại <b>UID</b>:",
                reply_markup=build_wizard_keyboard(),
            )
            return
        draft = DealDraft(uid=uid)
        await _start_wizard(message, state, draft, step=2)

    @router.message(WizardStates.waiting_note, F.text)
    async def wizard_step_note(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("wizard_draft", {}))
        draft.note = (message.text or "").strip() or "Theo dõi nhanh"
        await _start_wizard(message, state, draft, step=3)

    @router.message(WizardStates.waiting_price, F.text)
    async def wizard_step_price(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("wizard_draft", {}))
        draft.price = (message.text or "").strip() or "0"
        await _start_wizard(message, state, draft, step=4)

    @router.message(WizardStates.waiting_faq, F.text)
    async def wizard_step_faq(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("wizard_draft", {}))
        raw_faq = (message.text or "").strip()
        draft.faq = "" if raw_faq == "-" else raw_faq
        user_id = message.from_user.id if message.from_user else 0
        await _finalize_wizard(message, state, watch_service, draft, user_id)

    @router.callback_query(WizardCallback.filter())
    async def cb_wizard(
        query: CallbackQuery,
        callback_data: WizardCallback,
        state: FSMContext,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        if callback_data.action == "cancel":
            await state.clear()
            await _safe_edit(msg, "🛑 Đã hủy lên kèo.", build_main_menu_keyboard())
            await query.answer("Đã hủy.")
            return
        if callback_data.action == "skip_faq":
            data = await state.get_data()
            draft = DealDraft.model_validate(data.get("wizard_draft", {}))
            if not draft.uid:
                await state.clear()
                await query.answer("Phiên nhập đã hết hạn, vui lòng thử lại.", show_alert=True)
                return
            draft.faq = ""
            user_id = query.from_user.id if query.from_user else 0
            await _finalize_wizard(msg, state, watch_service, draft, user_id, edit_existing=True)
            await query.answer("✅ Đã lên kèo!")
            return
        await query.answer()

    @router.message(Command("addlist"))
    async def cmd_addlist(message: Message, state: FSMContext) -> None:
        await state.set_state(AddListStates.waiting_for_lines)
        await message.answer(
            "📥 <b>Thêm nhiều kèo cùng lúc</b>\n\n"
            "Gửi danh sách (mỗi dòng 1 kèo) hoặc file <code>.txt</code> theo mẫu:\n"
            "<code>UID | ghi chú | giá [| FAQ]</code>",
            reply_markup=build_cancel_keyboard(),
        )

    @router.message(AddListStates.waiting_for_lines, F.text)
    async def handle_addlist_text(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        await state.clear()
        user_id = message.from_user.id if message.from_user else 0
        added, errors = await watch_service.add_watch_batch(message.chat.id, user_id, text)
        lines = [f"✅ Đã thêm/cập nhật <b>{len(added)}</b> kèo thành công."]
        if errors:
            lines.append(f"⚠️ Có <b>{len(errors)}</b> dòng lỗi:")
            lines.extend(errors[:10])
        await message.answer("\n".join(lines), reply_markup=build_main_menu_keyboard())

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
        await message.answer("\n".join(lines), reply_markup=build_main_menu_keyboard())

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
    async def cmd_find(message: Message, command: CommandObject, state: FSMContext) -> None:
        query = (command.args or "").strip()
        if not query:
            await state.set_state(SearchStates.waiting_for_query)
            await message.answer(
                "🔍 Vui lòng nhập <b>UID</b>, mã <b>#ID</b> hoặc <b>từ khóa ghi chú</b> cần tìm:",
                reply_markup=build_cancel_keyboard(),
            )
            return
        items = await watch_service.search_watches(message.chat.id, query)
        await message.answer(
            format_search_results(query, items),
            reply_markup=build_main_menu_keyboard(),
        )

    @router.message(SearchStates.waiting_for_query, F.text)
    async def handle_search_query(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        await state.clear()
        items = await watch_service.search_watches(message.chat.id, query)
        await message.answer(
            format_search_results(query, items),
            reply_markup=build_main_menu_keyboard(),
        )

    @router.message(CheckOneStates.waiting_for_uid, F.text)
    async def handle_check_one_uid(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if bulk_service is None:
            await state.clear()
            await message.answer("⚠️ Chức năng check nhanh chưa sẵn sàng.")
            return
        try:
            uid, outcome, from_cache = await bulk_service.check_single_uid(raw)
        except ValueError as exc:
            await message.answer(
                f"❌ {exc}\nVui lòng gửi lại UID hợp lệ:", reply_markup=build_cancel_keyboard()
            )
            return
        await state.clear()
        markup = None
        if outcome != CheckOutcome.LIVE:
            from aiogram.types import InlineKeyboardButton

            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="➕ Lên kèo UID này",
                            callback_data=QuickWatchCallback(uid=uid).pack(),
                        )
                    ]
                ]
            )
        await message.answer(format_single_check(uid, outcome, from_cache), reply_markup=markup)

    @router.message(DealFormStates.waiting_for_field, F.text)
    async def handle_deal_form_field_input(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("deal_draft", {}))
        field = draft.editing_field
        raw_val = (message.text or "").strip()

        if field == "uid":
            uid = extract_uid(raw_val)
            if not uid:
                await message.answer(
                    "❌ UID không hợp lệ (cần 5–20 chữ số hoặc link <code>profile.php?id=...</code>). "
                    "Vui lòng gửi lại:",
                    reply_markup=build_field_prompt_keyboard(),
                )
                return
            draft.uid = uid
        elif field == "note":
            draft.note = raw_val
        elif field == "price":
            draft.price = raw_val
        elif field == "faq":
            draft.faq = "" if raw_val == "-" else raw_val

        draft.editing_field = None
        await state.set_state(DealFormStates.browsing)
        await state.update_data(deal_draft=draft.model_dump())

        # Refresh the main form message in-place if available, else send a new form message
        if draft.form_message_id is not None:
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=draft.form_message_id,
                    text=format_deal_form(draft),
                    reply_markup=build_deal_form_keyboard(draft),
                )
                with contextlib.suppress(TelegramBadRequest):
                    await message.delete()
                return
            except TelegramBadRequest:
                pass

        sent = await message.answer(
            format_deal_form(draft),
            reply_markup=build_deal_form_keyboard(draft),
        )
        draft.form_message_id = sent.message_id
        await state.update_data(deal_draft=draft.model_dump())

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
        if action in ("home", "cancel_fsm"):
            await state.clear()
            await _safe_edit(msg, WELCOME_TEXT, build_main_menu_keyboard())
        elif action == "add":
            await _start_wizard(msg, state, DealDraft(), step=1, edit_existing=True)
        elif action == "addlist":
            await state.set_state(AddListStates.waiting_for_lines)
            await _safe_edit(
                msg,
                "📥 <b>Thêm nhiều kèo cùng lúc</b>\n\n"
                "Gửi danh sách (mỗi dòng <code>UID | ghi chú | giá [| FAQ]</code>) "
                "hoặc gửi file <code>.txt</code>:",
                build_cancel_keyboard(),
            )
        elif action == "check_one":
            await state.set_state(CheckOneStates.waiting_for_uid)
            await _safe_edit(
                msg,
                "🔎 Vui lòng gửi <b>1 UID</b> hoặc <b>link Facebook</b> cần kiểm tra:",
                build_cancel_keyboard(),
            )
        elif action == "bulk":
            from src.modules.bulk.handlers import BulkStates

            await state.set_state(BulkStates.waiting_for_uids)
            await _safe_edit(
                msg,
                "📦 <b>Check hàng loạt (ARQ Worker)</b>\n\n"
                "Vui lòng gửi <b>danh sách nhiều dòng UID</b> hoặc <b>file .txt</b> (tối đa 50.000 UID):",
                build_cancel_keyboard(),
            )
        elif action == "find":
            await state.set_state(SearchStates.waiting_for_query)
            await _safe_edit(
                msg,
                "🔍 Vui lòng nhập <b>UID</b>, mã <b>#ID</b> hoặc <b>từ khóa ghi chú</b> cần tìm:",
                build_cancel_keyboard(),
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

    @router.callback_query(DealFormCallback.filter())
    async def cb_deal_form(
        query: CallbackQuery,
        callback_data: DealFormCallback,
        state: FSMContext,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return

        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("deal_draft", {}))
        draft.form_message_id = msg.message_id
        action = callback_data.action

        if action == "cancel":
            await state.clear()
            await _safe_edit(msg, "🛑 Đã hủy biểu mẫu kèo.", build_main_menu_keyboard())
            await query.answer("Đã hủy.")
            return

        if action == "back":
            draft.editing_field = None
            await state.set_state(DealFormStates.browsing)
            await state.update_data(deal_draft=draft.model_dump())
            await _safe_edit(msg, format_deal_form(draft), build_deal_form_keyboard(draft))
            await query.answer()
            return

        if action == "field":
            field = callback_data.field
            prompt = FIELD_PROMPTS.get(field)
            if not prompt:
                await query.answer("Trường không hợp lệ.", show_alert=True)
                return
            draft.editing_field = field
            await state.set_state(DealFormStates.waiting_for_field)
            await state.update_data(deal_draft=draft.model_dump())
            await _safe_edit(
                msg,
                f"{format_deal_form(draft)}\n\n👉 {prompt}",
                build_field_prompt_keyboard(),
            )
            await query.answer()
            return

        if action == "save":
            if not draft.uid:
                await query.answer("⚠️ Vui lòng bấm '🆔 Nhập UID *' trước!", show_alert=True)
                return

            user_id = query.from_user.id if query.from_user else 0
            try:
                if draft.is_edit and draft.watch_id is not None:
                    item = await watch_service.update_watch_details(
                        msg.chat.id,
                        draft.watch_id,
                        note=draft.note,
                        price=draft.price,
                        faq=draft.faq,
                    )
                    title = "✏️ Đã cập nhật kèo"
                else:
                    note = draft.note or "Theo dõi nhanh"
                    price = draft.price or "0"
                    raw_line = f"{draft.uid} | {note} | {price}"
                    if draft.faq:
                        raw_line += f" | {draft.faq}"
                    item, updated = await watch_service.upsert_watch(
                        msg.chat.id,
                        user_id,
                        raw_line,
                    )
                    title = "♻️ Đã cập nhật kèo" if updated else "📌 Đã lên kèo thành công"
            except ValueError as exc:
                await query.answer(str(exc), show_alert=True)
                return

            await state.clear()
            await _safe_edit(
                msg,
                format_watch_card(item, title=title),
                build_watch_keyboard(item),
            )
            await query.answer("✅ Đã lưu kèo!")
            return

        await query.answer()

    @router.callback_query(QuickWatchCallback.filter())
    async def cb_quick_watch(
        query: CallbackQuery,
        callback_data: QuickWatchCallback,
        state: FSMContext,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        await _start_wizard(msg, state, DealDraft(uid=callback_data.uid), step=2)
        await query.answer("📝 Đã điền sẵn UID — nhập ghi chú nhé!")

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
    async def cb_watch_action(
        query: CallbackQuery,
        callback_data: WatchActionCallback,
        state: FSMContext,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        action = callback_data.action
        watch_id = callback_data.watch_id
        try:
            if action == "edit":
                existing = await watch_service.get_watch(msg.chat.id, watch_id)
                draft = DealDraft(
                    watch_id=existing.id,
                    uid=existing.uid,
                    note=existing.note,
                    price=existing.price,
                    faq=existing.faq,
                    form_message_id=msg.message_id,
                )
                await _open_deal_form(msg, state, draft, edit_existing=True)
                await query.answer("✏️ Chọn mục cần sửa.")
                return
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


async def _start_wizard(
    message: Message,
    state: FSMContext,
    draft: DealDraft,
    *,
    step: int,
    edit_existing: bool = False,
) -> None:
    step_state = {
        1: WizardStates.waiting_uid,
        2: WizardStates.waiting_note,
        3: WizardStates.waiting_price,
        4: WizardStates.waiting_faq,
    }[step]
    await state.set_state(step_state)
    await state.update_data(wizard_draft=draft.model_dump())
    text = format_wizard_step(step, draft)
    markup = build_wizard_keyboard(allow_skip=(step == 4))
    if edit_existing:
        await _safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _finalize_wizard(
    message: Message,
    state: FSMContext,
    watch_service: WatchService,
    draft: DealDraft,
    user_id: int,
    *,
    edit_existing: bool = False,
) -> None:
    note = draft.note or "Theo dõi nhanh"
    price = draft.price or "0"
    raw_line = f"{draft.uid} | {note} | {price}"
    if draft.faq:
        raw_line += f" | {draft.faq}"

    await state.clear()
    try:
        item, updated = await watch_service.upsert_watch(message.chat.id, user_id, raw_line)
    except ValueError as exc:
        await message.answer(f"❌ {exc}", reply_markup=build_main_menu_keyboard())
        return

    title = "♻️ Đã cập nhật UID thành công!" if updated else "✅ Đã thêm UID thành công!"
    text = format_watch_card(item, title=title)
    markup = build_watch_keyboard(item)
    if edit_existing:
        await _safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _open_deal_form(
    message: Message,
    state: FSMContext,
    draft: DealDraft,
    *,
    edit_existing: bool = False,
) -> None:
    text = format_deal_form(draft)
    markup = build_deal_form_keyboard(draft)
    if edit_existing:
        await _safe_edit(message, text, markup)
        draft.form_message_id = message.message_id
    else:
        sent = await message.answer(text, reply_markup=markup)
        draft.form_message_id = sent.message_id
    await state.set_state(DealFormStates.browsing)
    await state.update_data(deal_draft=draft.model_dump())


async def _handle_upsert_watch(message: Message, watch_service: WatchService, raw: str) -> None:
    user_id = message.from_user.id if message.from_user else 0
    try:
        item, updated = await watch_service.upsert_watch(message.chat.id, user_id, raw)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    title = "♻️ Đã cập nhật UID thành công!" if updated else "✅ Đã thêm UID thành công!"
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
