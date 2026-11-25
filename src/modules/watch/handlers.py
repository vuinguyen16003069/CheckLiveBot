from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from src.core.ports import FacebookCheckerPort
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
    DealDraft,
    ListLookupStates,
    ListPageCallback,
    QuickWatchCallback,
    RecheckAllCallback,
    WatchActionCallback,
    WizardCallback,
    WizardStates,
    build_delete_confirm_keyboard,
    build_list_keyboard,
    build_reply_keyboard,
    build_watch_keyboard,
    build_wizard_keyboard,
    format_wizard_step,
    recover_draft_from_text,
)

logger = logging.getLogger(__name__)


async def _resolve_watch_photo(
    item: WatchItem,
    fetch_bytes: Callable[[str], Awaitable[bytes | None]] | None,
) -> str | BufferedInputFile | None:
    if item.avatar_file_id:
        return item.avatar_file_id
    if item.avatar_url and fetch_bytes is not None:
        avatar_bytes = await fetch_bytes(item.avatar_url)
        if avatar_bytes:
            return BufferedInputFile(avatar_bytes, filename=f"{item.uid}.jpg")
    return None


class TelegramWatchNotifier:
    def __init__(self, bot: Bot, *, checker: FacebookCheckerPort | None = None) -> None:
        self._bot = bot
        self._checker = checker

    async def notify_live(self, item: WatchItem) -> None:
        text = format_live_notification(item)
        markup = build_watch_keyboard(item)
        fetch_bytes = self._checker.fetch_avatar_bytes if self._checker is not None else None
        photo = await _resolve_watch_photo(item, fetch_bytes)

        async def _send() -> None:
            if photo is not None:
                try:
                    sent = await self._bot.send_photo(
                        chat_id=item.chat_id,
                        photo=photo,
                        caption=text,
                        reply_markup=markup,
                        has_spoiler=item.is_hidden,
                    )
                    if sent.photo:
                        item.avatar_file_id = sent.photo[-1].file_id
                    return
                except TelegramBadRequest:
                    pass
            await self._bot.send_message(chat_id=item.chat_id, text=text, reply_markup=markup)

        try:
            try:
                await _send()
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(float(exc.retry_after), 5.0))
                await _send()
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning(
                "watcher_notify_unreachable watch_id=%s chat_id=%s error=%s",
                item.id,
                item.chat_id,
                exc,
            )


def create_watch_router(watch_service: WatchService) -> Router:
    router = Router(name="watch")
    cached_bot_username: str | None = None
    bot_username_loaded = False

    async def render_list(
        chat_id: int, bot: Bot, status: str = "ALL", page: int = 1
    ) -> tuple[str, InlineKeyboardMarkup]:
        nonlocal cached_bot_username, bot_username_loaded
        status_filter = None if status == "ALL" else WatchStatus(status)
        items, total, total_pages = await watch_service.list_watches(
            chat_id, status_filter=status_filter, page=page
        )
        page = min(page, total_pages)
        if not bot_username_loaded:
            with contextlib.suppress(Exception):
                cached_bot_username = (await bot.me()).username
                bot_username_loaded = True
        text = format_watch_list(
            items,
            status_filter=status,
            page=page,
            total_pages=total_pages,
            total_items=total,
            bot_username=cached_bot_username,
        )
        return text, build_list_keyboard(status, page, total_pages)

    async def send_list(message: Message, state: FSMContext, bot: Bot) -> None:
        await state.set_state(ListLookupStates.waiting_uid)
        text, markup = await render_list(message.chat.id, bot)
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)

    async def run_menu(action: str, message: Message, state: FSMContext, bot: Bot) -> None:
        await state.clear()
        if action == "add":
            await _start_wizard(message, state, DealDraft(), step=1)
        elif action == "list":
            await send_list(message, state, bot)
        elif action == "check":
            await message.answer(
                "🔎 <b>Check nhanh LIVE / DIE</b>\n\n"
                "Dán thẳng <b>1 UID / link Facebook</b>, <b>nhiều dòng UID</b> hoặc gửi <b>file .txt</b> "
                "vào chat này — bot sẽ kiểm tra ngay!"
            )
        elif action == "stats":
            await message.answer(format_watch_stats(await watch_service.get_stats(message.chat.id)))

    @router.message(Command("start"))
    async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
        payload = (command.args or "").strip()

        if payload.startswith("w_") and payload[2:].isdigit():
            try:
                item = await watch_service.get_watch(message.chat.id, int(payload[2:]))
            except ValueError:
                await message.answer("⚠️ Không tìm thấy kèo này trong chat.")
                return
            with contextlib.suppress(TelegramBadRequest):
                await message.delete()
            await _answer_watch_card(
                message, item, reply_markup=_back_keyboard(item), watch_service=watch_service
            )
            return

        stats = await watch_service.get_stats(message.chat.id)
        await message.answer(
            format_welcome_dashboard(stats),
            reply_markup=build_reply_keyboard(),
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

    @router.message(F.text.in_({"➕ Lên kèo", "📋 Danh sách", "🔎 Check nhanh", "📊 Thống kê"}))
    async def handle_reply_shortcuts(message: Message, state: FSMContext, bot: Bot) -> None:
        action = {
            "➕ Lên kèo": "add",
            "📋 Danh sách": "list",
            "🔎 Check nhanh": "check",
            "📊 Thống kê": "stats",
        }[message.text or ""]
        await run_menu(action, message, state, bot)

    @router.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
        raw = (command.args or "").strip()
        if not raw:
            await _start_wizard(message, state, DealDraft(), step=1)
            return
        try:
            item = await watch_service.add_watch(message.chat.id, _user_id(message), raw)
        except DuplicateWatchError as exc:
            text, markup = _duplicate_view(exc)
            await message.answer(text, reply_markup=markup)
            return
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        await _answer_watch_card(
            message,
            item,
            title="✅ Đã thêm UID thành công!",
            reply_markup=build_watch_keyboard(item),
            watch_service=watch_service,
        )

    @router.message(WizardStates.waiting_uid, F.text)
    async def wizard_step_uid(message: Message, state: FSMContext, bot: Bot) -> None:
        draft = await _load_draft(state)
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
                reply_markup=_duplicate_keyboard(existing.id, in_wizard=True),
            )
            return
        draft.uid = uid
        await _advance_wizard(message, state, bot, draft, next_step=2)

    @router.message(WizardStates.waiting_note, F.text)
    async def wizard_step_note(message: Message, state: FSMContext, bot: Bot) -> None:
        draft = await _load_draft(state)
        draft.note = (message.text or "").strip() or "Theo dõi nhanh"
        await _advance_wizard(message, state, bot, draft, next_step=3)

    @router.message(WizardStates.waiting_price, F.text)
    async def wizard_step_price(message: Message, state: FSMContext, bot: Bot) -> None:
        draft = await _load_draft(state)
        draft.price = (message.text or "").strip() or "0"
        await _advance_wizard(message, state, bot, draft, next_step=4)

    @router.message(WizardStates.waiting_faq, F.text)
    async def wizard_step_faq(message: Message, state: FSMContext, bot: Bot) -> None:
        draft = await _load_draft(state)
        faq = (message.text or "").strip()
        draft.faq = "" if faq == "-" else faq
        await _finalize_wizard(message, state, watch_service, draft, _user_id(message), bot=bot)

    @router.callback_query(WizardCallback.filter())
    async def cb_wizard(
        query: CallbackQuery,
        callback_data: WizardCallback,
        state: FSMContext,
    ) -> None:
        msg = await _callback_message(query)
        if msg is None:
            return

        draft = await _load_draft(state)
        if not draft.uid:
            recovered = recover_draft_from_text(msg.text)
            if recovered.uid:
                draft = recovered
        draft.wizard_message_id = msg.message_id
        action = callback_data.action

        if action == "cancel":
            await state.clear()
            if draft.edit_watch_id is None:
                await _safe_edit(msg, "🛑 Đã hủy lên kèo.")
                await query.answer("🛑 Đã hủy lên kèo.")
                return
            try:
                item = await watch_service.get_watch(msg.chat.id, draft.edit_watch_id)
                await _safe_edit(msg, format_watch_card(item), build_watch_keyboard(item))
            except ValueError:
                await _safe_edit(msg, "🛑 Đã hủy sửa kèo.")
            await query.answer("🛑 Đã hủy sửa kèo.")
            return

        if action == "prev":
            prev_step = int(callback_data.value or 1)
            for field in ("uid", "note", "price")[prev_step - 1 :]:
                setattr(draft, field, "")
            await _start_wizard(msg, state, draft, step=prev_step, edit_existing=True)
            await query.answer()
            return

        if action not in ("pick_price", "pick_faq", "skip_faq"):
            await query.answer()
            return

        if not draft.uid:
            await state.clear()
            await query.answer("Phiên nhập đã hết hạn, vui lòng bấm Lên kèo lại.", show_alert=True)
            return

        if action == "pick_price":
            draft.price = callback_data.value
            await _start_wizard(msg, state, draft, step=4, edit_existing=True)
            await query.answer(f"💰 Đã chọn giá {callback_data.value}")
            return

        draft.faq = callback_data.value if action == "pick_faq" else ""
        toast = "✅ Đã cập nhật kèo!" if draft.edit_watch_id is not None else "✅ Đã lên kèo!"
        await _finalize_wizard(
            msg, state, watch_service, draft, query.from_user.id, edit_existing=True
        )
        await query.answer(toast)

    @router.message(Command("list"))
    async def cmd_list(message: Message, state: FSMContext, bot: Bot) -> None:
        await send_list(message, state, bot)

    @router.message(
        ListLookupStates.waiting_uid,
        F.text,
        ~F.text.contains("\n"),
        F.text.func(extract_uid),
    )
    async def handle_list_lookup(message: Message) -> None:
        uid = extract_uid((message.text or "").strip())
        if not uid:
            return
        item = await watch_service.find_by_uid(message.chat.id, uid)
        if item is None:
            await message.answer(
                f"📭 Không tìm thấy kèo nào có UID <code>{uid}</code> trong danh sách."
            )
            return
        await _answer_watch_card(
            message, item, reply_markup=_back_keyboard(item), watch_service=watch_service
        )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(format_watch_stats(await watch_service.get_stats(message.chat.id)))

    @router.callback_query(RecheckAllCallback.filter())
    async def cb_recheck_all(query: CallbackQuery) -> None:
        msg = await _callback_message(query)
        if msg is None:
            return
        scanned, turned_live = await watch_service.recheck_chat(msg.chat.id)
        if scanned == 0:
            with contextlib.suppress(TelegramBadRequest):
                await query.answer("📭 Không có kèo DIE nào cần quét lại.")
            return
        toast = (
            f"🔄 Đã quét {scanned} kèo DIE — {len(turned_live)} kèo về LIVE!"
            if turned_live
            else f"🔄 Đã quét {scanned} kèo DIE — chưa có kèo nào về LIVE."
        )
        with contextlib.suppress(TelegramBadRequest):
            await query.answer(toast)
        for item in turned_live:
            await _answer_watch_card(
                msg,
                item,
                title="🔔 🟢 THÔNG BÁO TÀI KHOẢN ĐÃ LIVE!",
                reply_markup=build_watch_keyboard(item),
                watch_service=watch_service,
            )

    @router.callback_query(QuickWatchCallback.filter())
    async def cb_quick_watch(
        query: CallbackQuery,
        callback_data: QuickWatchCallback,
        state: FSMContext,
    ) -> None:
        msg = await _callback_message(query)
        if msg is None:
            return
        existing = await watch_service.find_by_uid(msg.chat.id, callback_data.uid)
        if existing is not None:
            await msg.answer(
                format_watch_card(existing, title="⚠️ UID này đã có trong danh sách!"),
                reply_markup=build_watch_keyboard(existing),
            )
            await query.answer(f"⚠️ UID đã tồn tại ở kèo #{existing.id}!")
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
        msg = await _callback_message(query)
        if msg is None:
            return
        await state.set_state(ListLookupStates.waiting_uid)
        text, markup = await render_list(msg.chat.id, bot, callback_data.status, callback_data.page)
        if msg.photo:
            await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)
        else:
            await _safe_edit(msg, text, markup)
        await query.answer()

    @router.callback_query(WatchActionCallback.filter())
    async def cb_watch_action(
        query: CallbackQuery,
        callback_data: WatchActionCallback,
        state: FSMContext,
    ) -> None:
        msg = await _callback_message(query)
        if msg is None:
            return

        action = callback_data.action
        watch_id = callback_data.watch_id
        chat_id = msg.chat.id
        if action != "edit":
            await state.clear()

        try:
            if action == "edit":
                item = await watch_service.get_watch(chat_id, watch_id)
                await _start_wizard(
                    msg,
                    state,
                    DealDraft(uid=item.uid, edit_watch_id=item.id),
                    step=2,
                    edit_existing=True,
                )
                await query.answer("✏️ Nhập lại thông tin kèo!")
                return

            toast: str | None = None
            markup: InlineKeyboardMarkup | None

            if action == "open":
                item = await watch_service.get_watch(chat_id, watch_id)
                text, markup = format_watch_card(item), _back_keyboard(item)
            elif action == "delete":
                item = await watch_service.get_watch(chat_id, watch_id)
                text = (
                    f"{format_watch_card(item)}\n\n⚠️ <b>Bạn chắc chắn muốn xóa kèo #{item.id}?</b>"
                )
                markup = build_delete_confirm_keyboard(item.id)
                toast = "Bấm xác nhận để xóa."
            elif action == "delete_confirm":
                deleted = await watch_service.delete_watch(chat_id, watch_id)
                text = f"🗑 Đã xóa kèo <b>#{deleted.id}</b> (<code>{deleted.uid}</code>)."
                markup = None
                toast = "🗑 Đã xóa kèo."
            elif action == "delete_cancel":
                item = await watch_service.get_watch(chat_id, watch_id)
                text, markup = format_watch_card(item), build_watch_keyboard(item)
                toast = "Đã giữ lại kèo."
            elif action in ("hide", "show"):
                hidden = action == "hide"
                item = await watch_service.set_hidden(chat_id, watch_id, hidden)
                text, markup = format_watch_card(item), build_watch_keyboard(item)
                toast = "🙈 Đã che mờ thông tin." if hidden else "🐵 Đã hiện thông tin."
                if msg.photo:
                    with contextlib.suppress(TelegramBadRequest):
                        await msg.edit_media(
                            media=InputMediaPhoto(
                                media=msg.photo[-1].file_id,
                                caption=text,
                                has_spoiler=hidden,
                            ),
                            reply_markup=markup,
                        )
                    await query.answer(toast)
                    return
            elif action in ("done", "cancel", "continue"):
                status, toast = {
                    "done": (WatchStatus.DONE, "✅ Đã đánh dấu Done kèo."),
                    "cancel": (WatchStatus.CANCELLED, "❌ Đã hủy kèo."),
                    "continue": (WatchStatus.DIE, "🔄 Đã tiếp tục theo dõi kèo."),
                }[action]
                item = await watch_service.mark_status(chat_id, watch_id, status)
                text, markup = format_watch_card(item), build_watch_keyboard(item)
            else:
                await query.answer("Hành động không hợp lệ.", show_alert=True)
                return
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        await _safe_edit(msg, text, markup)
        await query.answer(toast)

    return router


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else 0


async def _callback_message(query: CallbackQuery) -> Message | None:
    if isinstance(query.message, Message):
        return query.message
    await query.answer()
    return None


def _back_keyboard(item: WatchItem) -> InlineKeyboardMarkup:
    return build_watch_keyboard(item, back_status="ALL", back_page=1)


async def _load_draft(state: FSMContext) -> DealDraft:
    data = await state.get_data()
    return DealDraft.model_validate(data.get("wizard_draft", {}))


async def _save_draft(state: FSMContext, step: int, draft: DealDraft) -> None:
    await state.set_state(
        {
            1: WizardStates.waiting_uid,
            2: WizardStates.waiting_note,
            3: WizardStates.waiting_price,
            4: WizardStates.waiting_faq,
        }[step]
    )
    await state.update_data(wizard_draft=draft.model_dump())


def _step_view(step: int, draft: DealDraft) -> tuple[str, InlineKeyboardMarkup]:
    min_step = 2 if draft.edit_watch_id is not None else 1
    return format_wizard_step(step, draft), build_wizard_keyboard(step=step, min_step=min_step)


async def _start_wizard(
    message: Message,
    state: FSMContext,
    draft: DealDraft,
    *,
    step: int,
    edit_existing: bool = False,
) -> None:
    text, markup = _step_view(step, draft)
    if edit_existing:
        await _safe_edit(message, text, markup)
        draft.wizard_message_id = message.message_id
    else:
        draft.wizard_message_id = (await message.answer(text, reply_markup=markup)).message_id
    await _save_draft(state, step, draft)


async def _show(
    message: Message,
    bot: Bot | None,
    draft: DealDraft,
    text: str,
    markup: InlineKeyboardMarkup,
) -> Message | None:
    if bot is not None and draft.wizard_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=draft.wizard_message_id,
                text=text,
                reply_markup=markup,
            )
        except TelegramBadRequest:
            pass
        else:
            with contextlib.suppress(TelegramBadRequest):
                await message.delete()
            return None
    return await message.answer(text, reply_markup=markup)


async def _advance_wizard(
    message: Message,
    state: FSMContext,
    bot: Bot,
    draft: DealDraft,
    *,
    next_step: int,
) -> None:
    text, markup = _step_view(next_step, draft)
    sent = await _show(message, bot, draft, text, markup)
    if sent is not None:
        draft.wizard_message_id = sent.message_id
    await _save_draft(state, next_step, draft)


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

    async def show(text: str, markup: InlineKeyboardMarkup) -> None:
        if edit_existing:
            await _safe_edit(message, text, markup)
        else:
            await _show(message, bot, draft, text, markup)

    try:
        if draft.edit_watch_id is not None:
            item = await watch_service.update_watch(
                message.chat.id, draft.edit_watch_id, note=note, price=price, faq=draft.faq
            )
            title = "✅ Đã cập nhật kèo thành công!"
        else:
            raw_line = " | ".join(part for part in (draft.uid, note, price, draft.faq) if part)
            item = await watch_service.add_watch(message.chat.id, user_id, raw_line)
            title = "✅ Đã thêm UID thành công!"
    except DuplicateWatchError as exc:
        await show(*_duplicate_view(exc))
        return
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    await show(format_watch_card(item, title=title), build_watch_keyboard(item))


def _duplicate_view(exc: DuplicateWatchError) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"⚠️ {exc}\nBấm nút dưới để mở kèo cũ:",
        _duplicate_keyboard(exc.existing.id, in_wizard=False),
    )


def _duplicate_keyboard(watch_id: int, *, in_wizard: bool) -> InlineKeyboardMarkup:
    row = [
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


async def _answer_watch_card(
    message: Message,
    item: WatchItem,
    *,
    title: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    watch_service: WatchService | None = None,
) -> None:
    text = format_watch_card(item, title=title)
    if item.status != WatchStatus.DIE:
        fetch_bytes = watch_service.fetch_avatar_bytes if watch_service is not None else None
        photo = await _resolve_watch_photo(item, fetch_bytes)
        if photo is not None:
            try:
                sent = await message.answer_photo(
                    photo=photo,
                    caption=text,
                    reply_markup=reply_markup,
                    has_spoiler=item.is_hidden,
                )
                if sent.photo and watch_service is not None:
                    with contextlib.suppress(Exception):
                        await watch_service.save_avatar_file_id(item, sent.photo[-1].file_id)
                return
            except TelegramBadRequest:
                pass
    await message.answer(text, reply_markup=reply_markup)


async def _safe_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    with contextlib.suppress(TelegramBadRequest):
        if message.photo:
            await message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
