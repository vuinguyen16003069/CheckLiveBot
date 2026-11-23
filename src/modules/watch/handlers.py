from __future__ import annotations

import contextlib

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.modules.bulk.domain import extract_uid
from src.modules.watch.domain import (
    WatchItem,
    WatchStatus,
    format_live_notification,
    format_watch_card,
    format_watch_list,
    format_watch_stats,
    format_welcome_dashboard,
)
from src.modules.watch.service import DuplicateWatchError, WatchService
from src.modules.watch.ui import (
    REPLY_BTN_ADD,
    REPLY_BTN_CHECK,
    REPLY_BTN_LIST,
    REPLY_BTN_STATS,
    REPLY_SHORTCUTS,
    DashboardCallback,
    DealDraft,
    ListLookupStates,
    ListPageCallback,
    QuickWatchCallback,
    RecheckAllCallback,
    WatchActionCallback,
    WizardCallback,
    WizardStates,
    build_dashboard_keyboard,
    build_delete_confirm_keyboard,
    build_list_keyboard,
    build_reply_keyboard,
    build_watch_keyboard,
    build_wizard_keyboard,
    format_wizard_step,
    recover_draft_from_text,
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
    bot_username_cache: dict[str, str | None] = {"value": None}

    async def _get_bot_username(bot: Bot) -> str | None:
        if bot_username_cache["value"] is None:
            with contextlib.suppress(Exception):
                me = await bot.get_me()
                bot_username_cache["value"] = me.username
        return bot_username_cache["value"]

    @router.message(Command("start"))
    async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
        payload = (command.args or "").strip()
        if payload.startswith("w_") and payload[2:].isdigit():
            watch_id = int(payload[2:])
            try:
                item = await watch_service.get_watch(message.chat.id, watch_id)
            except ValueError:
                await message.answer("⚠️ Không tìm thấy kèo này trong chat.")
                return
            with contextlib.suppress(TelegramBadRequest):
                await message.delete()
            await message.answer(
                format_watch_card(item),
                reply_markup=build_watch_keyboard(item, back_status="ALL", back_page=1),
            )
            return

        stats = await watch_service.get_stats(message.chat.id)
        await message.answer(
            format_welcome_dashboard(stats),
            reply_markup=build_dashboard_keyboard(),
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "📖 <b>Hướng dẫn sử dụng CheckLiveBot</b>\n\n"
            "<b>1. Dùng bằng 4 nút ở đáy màn hình</b>\n"
            "• <b>➕ Lên kèo</b> — Nhập tuần tự <code>UID → Ghi chú → Giá → FAQ</code>\n"
            "• <b>📋 Danh sách</b> — Xem kèo (chạm thẳng vào UID để mở chi tiết)\n"
            "• <b>🔎 Check nhanh</b> — Check 1 UID, link Facebook, nhiều dòng UID hoặc file <code>.txt</code>\n"
            "• <b>📊 Thống kê</b> — Xem số lượng kèo theo trạng thái\n\n"
            "<b>2. Dán thẳng vào chat (không cần gõ lệnh)</b>\n"
            "• Dán 1 UID / link FB → Check ngay\n"
            "• Dán nhiều dòng UID hoặc gửi file <code>.txt</code> → Check hàng loạt (trả 1 file kết quả)\n\n"
            "<b>3. Lệnh nhanh</b>\n"
            "• <code>/add UID | ghi chú | giá | FAQ</code>\n"
            "• <code>/check &lt;UID / link FB&gt;</code> | <code>/list</code> | <code>/stats</code>",
            reply_markup=build_reply_keyboard(),
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("🛑 Đã hủy thao tác hiện tại.", reply_markup=build_reply_keyboard())

    @router.message(F.text.in_(REPLY_SHORTCUTS))
    async def handle_reply_shortcuts(message: Message, state: FSMContext, bot: Bot) -> None:
        await state.clear()
        text = message.text or ""
        if text == REPLY_BTN_ADD:
            await _start_wizard(message, state, DealDraft(), step=1)
        elif text == REPLY_BTN_LIST:
            await _send_list_message(message, state, watch_service, await _get_bot_username(bot))
        elif text == REPLY_BTN_CHECK:
            await message.answer(
                "🔎 <b>Check nhanh LIVE / DIE</b>\n\n"
                "Dán thẳng <b>1 UID / link Facebook</b>, <b>nhiều dòng UID</b> hoặc gửi <b>file .txt</b> "
                "vào chat này — bot sẽ kiểm tra ngay!",
            )
        elif text == REPLY_BTN_STATS:
            stats = await watch_service.get_stats(message.chat.id)
            await message.answer(format_watch_stats(stats))

    @router.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject, state: FSMContext) -> None:
        raw = (command.args or "").strip()
        if not raw:
            await _start_wizard(message, state, DealDraft(), step=1)
            return
        user_id = message.from_user.id if message.from_user else 0
        try:
            item = await watch_service.add_watch(message.chat.id, user_id, raw)
        except DuplicateWatchError as exc:
            await message.answer(
                f"⚠️ {exc}\nBấm nút dưới để mở kèo cũ:",
                reply_markup=_duplicate_watch_keyboard(exc.existing.id, in_wizard=False),
            )
            return
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        await message.answer(
            format_watch_card(item, title="✅ Đã thêm UID thành công!"),
            reply_markup=build_watch_keyboard(item),
        )

    @router.message(WizardStates.waiting_uid, F.text)
    async def wizard_step_uid(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("wizard_draft", {}))
        uid = extract_uid((message.text or "").strip())
        if not uid:
            await message.answer(
                "❌ UID chưa đúng (cần 1–20 chữ số, VD: <code>100045216769361</code> "
                "hoặc link Facebook cá nhân).\n👉 Gửi lại <b>UID</b>:",
                reply_markup=build_wizard_keyboard(step=1),
            )
            return
        existing = await watch_service.find_by_uid(message.chat.id, uid)
        if existing is not None:
            await message.answer(
                f"⚠️ UID <code>{uid}</code> đã tồn tại ở kèo <b>#{existing.id}</b> "
                f"({existing.status.value})!\n"
                "👉 Gửi <b>UID khác</b> hoặc bấm nút dưới để mở kèo cũ:",
                reply_markup=_duplicate_watch_keyboard(existing.id, in_wizard=True),
            )
            return
        draft.uid = uid
        await _advance_wizard_in_place(message, state, bot, draft, next_step=2)

    @router.message(WizardStates.waiting_note, F.text)
    async def wizard_step_note(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("wizard_draft", {}))
        draft.note = (message.text or "").strip() or "Theo dõi nhanh"
        await _advance_wizard_in_place(message, state, bot, draft, next_step=3)

    @router.message(WizardStates.waiting_price, F.text)
    async def wizard_step_price(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("wizard_draft", {}))
        draft.price = (message.text or "").strip() or "0"
        await _advance_wizard_in_place(message, state, bot, draft, next_step=4)

    @router.message(WizardStates.waiting_faq, F.text)
    async def wizard_step_faq(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("wizard_draft", {}))
        raw_faq = (message.text or "").strip()
        draft.faq = "" if raw_faq == "-" else raw_faq
        user_id = message.from_user.id if message.from_user else 0
        await _finalize_wizard(message, state, watch_service, draft, user_id, bot=bot)

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
        data = await state.get_data()
        draft = DealDraft.model_validate(data.get("wizard_draft", {}))
        if not draft.uid:
            recovered = recover_draft_from_text(msg.text)
            if recovered.uid:
                draft = recovered
        draft.wizard_message_id = msg.message_id
        action = callback_data.action

        if action == "cancel":
            await state.clear()
            if draft.edit_watch_id is not None:
                try:
                    item = await watch_service.get_watch(msg.chat.id, draft.edit_watch_id)
                    await _safe_edit(msg, format_watch_card(item), build_watch_keyboard(item))
                except ValueError:
                    await _safe_edit(msg, "🛑 Đã hủy sửa kèo.", None)
                await query.answer("🛑 Đã hủy sửa kèo.")
                return
            await _safe_edit(msg, "🛑 Đã hủy lên kèo.", None)
            await query.answer("🛑 Đã hủy lên kèo.")
            return

        if action == "prev":
            prev_step = int(callback_data.value or 1)
            if prev_step == 1:
                draft.uid = ""
                draft.note = ""
                draft.price = ""
            elif prev_step == 2:
                draft.note = ""
                draft.price = ""
            elif prev_step == 3:
                draft.price = ""
            await _start_wizard(msg, state, draft, step=prev_step, edit_existing=True)
            await query.answer()
            return

        if action == "pick_price":
            if not draft.uid:
                await state.clear()
                await query.answer(
                    "Phiên nhập đã hết hạn, vui lòng bấm Lên kèo lại.", show_alert=True
                )
                return
            draft.price = callback_data.value
            await _start_wizard(msg, state, draft, step=4, edit_existing=True)
            await query.answer(f"💰 Đã chọn giá {callback_data.value}")
            return

        if action in ("pick_faq", "skip_faq"):
            if not draft.uid:
                await state.clear()
                await query.answer("Phiên nhập đã hết hạn, vui lòng thử lại.", show_alert=True)
                return
            draft.faq = callback_data.value if action == "pick_faq" else ""
            user_id = query.from_user.id if query.from_user else 0
            toast = "✅ Đã cập nhật kèo!" if draft.edit_watch_id is not None else "✅ Đã lên kèo!"
            await _finalize_wizard(msg, state, watch_service, draft, user_id, edit_existing=True)
            await query.answer(toast)
            return

        await query.answer()

    @router.message(Command("list"))
    async def cmd_list(message: Message, state: FSMContext, bot: Bot) -> None:
        await _send_list_message(message, state, watch_service, await _get_bot_username(bot))

    @router.message(ListLookupStates.waiting_uid, F.text, ~F.text.startswith("/"))
    async def handle_list_lookup(message: Message) -> None:
        raw = (message.text or "").strip()
        uid = extract_uid(raw)
        if not uid:
            await message.answer(
                "❌ UID chưa đúng (cần 1–20 chữ số hoặc link Facebook cá nhân).\n"
                "👉 Nhắn lại <b>UID / link</b> để tra cứu kèo."
            )
            return
        item = await watch_service.find_by_uid(message.chat.id, uid)
        if item is None:
            await message.answer(
                f"📭 Không tìm thấy kèo nào có UID <code>{uid}</code> trong danh sách."
            )
            return
        await message.answer(
            format_watch_card(item),
            reply_markup=build_watch_keyboard(item, back_status="ALL", back_page=1),
        )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message, state: FSMContext) -> None:
        await state.clear()
        stats = await watch_service.get_stats(message.chat.id)
        await message.answer(format_watch_stats(stats))

    @router.callback_query(DashboardCallback.filter())
    async def cb_dashboard(
        query: CallbackQuery,
        callback_data: DashboardCallback,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        await state.clear()
        action = callback_data.action
        if action == "add":
            await _start_wizard(msg, state, DealDraft(), step=1)
        elif action == "list":
            await _send_list_message(msg, state, watch_service, await _get_bot_username(bot))
        elif action == "check":
            await msg.answer(
                "🔎 <b>Check nhanh LIVE / DIE</b>\n\n"
                "Dán thẳng <b>1 UID / link Facebook</b>, <b>nhiều dòng UID</b> hoặc gửi <b>file .txt</b> "
                "vào chat này — bot sẽ kiểm tra ngay!",
            )
        elif action == "stats":
            stats = await watch_service.get_stats(msg.chat.id)
            await msg.answer(format_watch_stats(stats))
        await query.answer()

    @router.callback_query(RecheckAllCallback.filter())
    async def cb_recheck_all(query: CallbackQuery) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        scanned, turned_live = await watch_service.recheck_chat(msg.chat.id)
        if scanned == 0:
            await query.answer("📭 Không có kèo DIE nào cần quét lại.", show_alert=False)
            return
        await query.answer(
            f"🔄 Đã quét {scanned} kèo DIE — {len(turned_live)} kèo về LIVE!",
            show_alert=False,
        )
        for item in turned_live:
            await msg.answer(
                format_live_notification(item),
                reply_markup=build_watch_keyboard(item),
            )

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
        existing = await watch_service.find_by_uid(msg.chat.id, callback_data.uid)
        if existing is not None:
            await msg.answer(
                format_watch_card(existing, title="⚠️ UID này đã có trong danh sách!"),
                reply_markup=build_watch_keyboard(existing),
            )
            await query.answer(f"⚠️ UID đã tồn tại ở kèo #{existing.id}!", show_alert=False)
            return
        await _start_wizard(msg, state, DealDraft(uid=callback_data.uid), step=2)
        await query.answer("📝 Đã điền sẵn UID — nhập ghi chú nhé!")

    @router.callback_query(ListPageCallback.filter())
    async def cb_list_page(
        query: CallbackQuery,
        callback_data: ListPageCallback,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        await state.set_state(ListLookupStates.waiting_uid)
        status_filter = WatchStatus(callback_data.status) if callback_data.status != "ALL" else None
        items, total, total_pages = await watch_service.list_watches(
            msg.chat.id,
            status_filter=status_filter,
            page=callback_data.page,
        )
        safe_page = min(callback_data.page, total_pages)
        text = format_watch_list(
            items,
            status_filter=callback_data.status,
            page=safe_page,
            total_pages=total_pages,
            total_items=total,
            bot_username=await _get_bot_username(bot),
        )
        await _safe_edit(
            msg,
            text,
            build_list_keyboard(callback_data.status, safe_page, total_pages),
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
        if action != "edit":
            await state.clear()
        try:
            if action == "open":
                item = await watch_service.get_watch(msg.chat.id, watch_id)
                await _safe_edit(
                    msg,
                    format_watch_card(item),
                    build_watch_keyboard(item, back_status="ALL", back_page=1),
                )
                await query.answer()
                return
            if action == "edit":
                item = await watch_service.get_watch(msg.chat.id, watch_id)
                await _start_wizard(
                    msg,
                    state,
                    DealDraft(uid=item.uid, edit_watch_id=item.id),
                    step=2,
                    edit_existing=True,
                )
                await query.answer("✏️ Nhập lại thông tin kèo!")
                return
            if action == "delete":
                existing = await watch_service.get_watch(msg.chat.id, watch_id)
                await _safe_edit(
                    msg,
                    f"{format_watch_card(existing)}\n\n⚠️ <b>Bạn chắc chắn muốn xóa kèo #{existing.id}?</b>",
                    build_delete_confirm_keyboard(existing.id),
                )
                await query.answer("Bấm xác nhận để xóa.")
                return
            if action == "delete_confirm":
                deleted = await watch_service.delete_watch(msg.chat.id, watch_id)
                await _safe_edit(
                    msg,
                    f"🗑 Đã xóa kèo <b>#{deleted.id}</b> (<code>{deleted.uid}</code>).",
                    None,
                )
                await query.answer("🗑 Đã xóa kèo.")
                return
            if action == "delete_cancel":
                item = await watch_service.get_watch(msg.chat.id, watch_id)
                await _safe_edit(msg, format_watch_card(item), build_watch_keyboard(item))
                await query.answer("Đã giữ lại kèo.")
                return
            if action == "hide":
                item = await watch_service.set_hidden(msg.chat.id, watch_id, True)
                toast = "🙈 Đã che mờ thông tin."
            elif action == "show":
                item = await watch_service.set_hidden(msg.chat.id, watch_id, False)
                toast = "🐵 Đã hiện thông tin."
            elif action == "done":
                item = await watch_service.mark_status(msg.chat.id, watch_id, WatchStatus.DONE)
                toast = "✅ Đã đánh dấu Done kèo."
            elif action == "cancel":
                item = await watch_service.mark_status(msg.chat.id, watch_id, WatchStatus.CANCELLED)
                toast = "❌ Đã hủy kèo."
            elif action == "continue":
                item = await watch_service.mark_status(msg.chat.id, watch_id, WatchStatus.DIE)
                toast = "🔄 Đã tiếp tục theo dõi kèo."
            else:
                await query.answer("Hành động không hợp lệ.", show_alert=True)
                return
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        await _safe_edit(msg, format_watch_card(item), build_watch_keyboard(item))
        await query.answer(toast)

    return router


async def _send_list_message(
    message: Message,
    state: FSMContext,
    watch_service: WatchService,
    bot_username: str | None = None,
) -> None:
    await state.set_state(ListLookupStates.waiting_uid)
    items, total, total_pages = await watch_service.list_watches(message.chat.id, page=1)
    text = format_watch_list(
        items,
        status_filter="ALL",
        page=1,
        total_pages=total_pages,
        total_items=total,
        bot_username=bot_username,
    )
    await message.answer(
        text,
        reply_markup=build_list_keyboard("ALL", 1, total_pages),
        disable_web_page_preview=True,
    )


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
    text = format_wizard_step(step, draft)
    min_step = 2 if draft.edit_watch_id is not None else 1
    markup = build_wizard_keyboard(step=step, min_step=min_step)
    if edit_existing:
        await _safe_edit(message, text, markup)
        draft.wizard_message_id = message.message_id
    else:
        sent = await message.answer(text, reply_markup=markup)
        draft.wizard_message_id = sent.message_id
    await state.set_state(step_state)
    await state.update_data(wizard_draft=draft.model_dump())


async def _advance_wizard_in_place(
    message: Message,
    state: FSMContext,
    bot: Bot,
    draft: DealDraft,
    *,
    next_step: int,
) -> None:
    step_state = {
        1: WizardStates.waiting_uid,
        2: WizardStates.waiting_note,
        3: WizardStates.waiting_price,
        4: WizardStates.waiting_faq,
    }[next_step]
    text = format_wizard_step(next_step, draft)
    min_step = 2 if draft.edit_watch_id is not None else 1
    markup = build_wizard_keyboard(step=next_step, min_step=min_step)

    if draft.wizard_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=draft.wizard_message_id,
                text=text,
                reply_markup=markup,
            )
            with contextlib.suppress(TelegramBadRequest):
                await message.delete()
            await state.set_state(step_state)
            await state.update_data(wizard_draft=draft.model_dump())
            return
        except TelegramBadRequest:
            pass

    sent = await message.answer(text, reply_markup=markup)
    draft.wizard_message_id = sent.message_id
    await state.set_state(step_state)
    await state.update_data(wizard_draft=draft.model_dump())


async def _finalize_wizard(
    message: Message,
    state: FSMContext,
    watch_service: WatchService,
    draft: DealDraft,
    user_id: int,
    *,
    edit_existing: bool = False,
    bot: Bot | None = None,
) -> None:
    note = draft.note or "Theo dõi nhanh"
    price = draft.price or "0"

    await state.clear()
    if draft.edit_watch_id is not None:
        try:
            item = await watch_service.update_watch(
                message.chat.id,
                draft.edit_watch_id,
                note=note,
                price=price,
                faq=draft.faq,
            )
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        text = format_watch_card(item, title="✅ Đã cập nhật kèo thành công!")
    else:
        raw_line = f"{draft.uid} | {note} | {price}"
        if draft.faq:
            raw_line += f" | {draft.faq}"
        try:
            item = await watch_service.add_watch(message.chat.id, user_id, raw_line)
        except DuplicateWatchError as exc:
            text = f"⚠️ {exc}\nBấm nút dưới để mở kèo cũ:"
            markup = _duplicate_watch_keyboard(exc.existing.id, in_wizard=False)
            if edit_existing:
                await _safe_edit(message, text, markup)
            else:
                await message.answer(text, reply_markup=markup)
            return
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        text = format_watch_card(item, title="✅ Đã thêm UID thành công!")

    markup = build_watch_keyboard(item)

    if edit_existing:
        await _safe_edit(message, text, markup)
        return

    if bot is not None and draft.wizard_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=draft.wizard_message_id,
                text=text,
                reply_markup=markup,
            )
            with contextlib.suppress(TelegramBadRequest):
                await message.delete()
            return
        except TelegramBadRequest:
            pass

    await message.answer(text, reply_markup=markup)


def _duplicate_watch_keyboard(watch_id: int, *, in_wizard: bool) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text=f"📌 Mở kèo #{watch_id}",
            callback_data=WatchActionCallback(action="open", watch_id=watch_id).pack(),
        )
    ]
    if in_wizard:
        row.append(
            InlineKeyboardButton(
                text="❌ Hủy",
                callback_data=WizardCallback(action="cancel").pack(),
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def _safe_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
