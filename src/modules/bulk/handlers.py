from __future__ import annotations

import contextlib
import io
from collections.abc import Sequence

import structlog
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.core.ports import CheckOutcome
from src.modules.bulk.domain import (
    build_combined_result_file,
    extract_uid,
    extract_uids_from_text,
    format_single_check,
)
from src.modules.bulk.service import BulkCheckService
from src.modules.watch.ui import QuickWatchCallback

logger = structlog.get_logger(__name__)


class RecheckSingleCallback(CallbackData, prefix="rc"):
    uid: str


class TelegramBulkNotifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def update_progress(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except TelegramRetryAfter as exc:
            logger.warning("telegram_progress_rate_limited", retry_after=exc.retry_after)
        except TelegramBadRequest:
            pass
        except Exception as exc:
            logger.warning("telegram_progress_update_failed", error=str(exc))

    async def send_bulk_results(
        self,
        chat_id: int,
        caption: str,
        live_uids: Sequence[str],
        die_uids: Sequence[str],
        error_uids: Sequence[str],
        job_id: str,
        status_message_id: int | None = None,
    ) -> None:
        if status_message_id is not None:
            with contextlib.suppress(TelegramBadRequest):
                await self._bot.delete_message(chat_id=chat_id, message_id=status_message_id)

        filename, content = build_combined_result_file(live_uids, die_uids, error_uids, job_id)
        if not content:
            await self._bot.send_message(chat_id=chat_id, text=caption)
            return

        doc = BufferedInputFile(content, filename=filename)
        await self._bot.send_document(chat_id=chat_id, document=doc, caption=caption)


def build_single_check_markup(uid: str, outcome: CheckOutcome) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if outcome != CheckOutcome.LIVE:
        row.append(
            InlineKeyboardButton(
                text="➕ Lên kèo UID này",
                callback_data=QuickWatchCallback(uid=uid).pack(),
            )
        )
    row.append(
        InlineKeyboardButton(
            text="🔄 Check lại",
            callback_data=RecheckSingleCallback(uid=uid).pack(),
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def create_bulk_router(bulk_service: BulkCheckService) -> Router:
    """Single unified check router: /check, paste 1 UID, paste multi-line UIDs, or send .txt."""
    router = Router(name="bulk")

    @router.message(Command("check"))
    async def cmd_check(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "🔎 <b>Kiểm tra nhanh LIVE / DIE</b>\n"
                "• Dán thẳng <b>1 UID</b> hoặc <b>link FB</b> để check ngay.\n"
                "• Dán <b>nhiều dòng UID</b> hoặc gửi file <code>.txt</code> để check hàng loạt.\n"
                "• Hoặc gõ: <code>/check 20531316728</code>"
            )
            return
        await _reply_single_check(message, bulk_service, raw)

    @router.callback_query(RecheckSingleCallback.filter())
    async def cb_recheck_single(
        query: CallbackQuery,
        callback_data: RecheckSingleCallback,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        try:
            uid, outcome = await bulk_service.check_single_uid(callback_data.uid)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text(
                format_single_check(uid, outcome),
                reply_markup=build_single_check_markup(uid, outcome),
            )
        await query.answer(f"🔄 Kết quả mới nhất: {outcome.value}")

    @router.message(F.document)
    async def handle_txt_document(message: Message, bot: Bot, state: FSMContext) -> None:
        document = message.document
        if document is None:
            return
        filename = (document.file_name or "").lower()
        if not filename.endswith(".txt"):
            return
        if document.file_size and document.file_size > 10 * 1024 * 1024:
            await message.answer("❌ File quá lớn (tối đa 10 MB).")
            return

        if await state.get_state() is not None:
            await state.clear()

        buf = io.BytesIO()
        await bot.download(document, destination=buf)
        text = buf.getvalue().decode("utf-8", errors="ignore")
        await _start_bulk(message, bulk_service, text)

    @router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
    async def handle_smart_plain_text(message: Message) -> None:
        """Paste 1 UID/FB link to check immediately, or 2+ UIDs to bulk check."""
        text = (message.text or "").strip()
        if not text:
            return
        single = extract_uid(text)
        if single:
            await _reply_single_check(message, bulk_service, single)
            return
        if "\n" in text:
            uids, _ = extract_uids_from_text(text, max_uids=50000)
            if len(uids) == 1:
                await _reply_single_check(message, bulk_service, uids[0])
            elif len(uids) >= 2:
                await _start_bulk(message, bulk_service, text)

    return router


async def _reply_single_check(
    message: Message,
    bulk_service: BulkCheckService,
    raw: str,
) -> None:
    if message.bot is not None:
        with contextlib.suppress(Exception):
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    try:
        uid, outcome = await bulk_service.check_single_uid(raw)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    await message.answer(
        format_single_check(uid, outcome),
        reply_markup=build_single_check_markup(uid, outcome),
    )


async def _start_bulk(message: Message, bulk_service: BulkCheckService, text: str) -> None:
    if message.bot is None:
        return
    try:
        job_id, uids, skipped = bulk_service.prepare_bulk_text(text)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    extra = f" (đã lọc bỏ {skipped:,} dòng trùng/lỗi)" if skipped else ""
    status_msg = await message.answer(
        f"📥 <b>Đã nhận {len(uids):,} UID</b>{extra}\n"
        f"🆔 Mã phiên: <code>#{job_id}</code>\n"
        "⚙️ Bot đang kiểm tra ngay và sẽ cập nhật tiến độ liên tục..."
    )
    bulk_service.start_bulk_uids(
        job_id=job_id,
        chat_id=message.chat.id,
        status_message_id=status_msg.message_id,
        uids=uids,
        notifier=TelegramBulkNotifier(message.bot),
    )
