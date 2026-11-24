from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from collections.abc import Sequence

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
    format_bulk_progress,
    format_single_check,
)
from src.modules.bulk.service import BulkCheckService
from src.modules.watch.ui import ListLookupStates, QuickWatchCallback

logger = logging.getLogger(__name__)


class RecheckSingleCallback(CallbackData, prefix="rc"):
    uid: str


class CancelBulkCallback(CallbackData, prefix="cb"):
    job_id: str


def build_bulk_progress_markup(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛑 Dừng check",
                    callback_data=CancelBulkCallback(job_id=job_id).pack(),
                )
            ]
        ]
    )


class TelegramBulkNotifier:
    def __init__(self, bot: Bot, *, job_id: str | None = None) -> None:
        self._bot = bot
        self._job_id = job_id
        self._retry_until = 0.0

    async def update_progress(self, chat_id: int, message_id: int, text: str) -> None:
        if time.monotonic() < self._retry_until:
            return
        reply_markup = build_bulk_progress_markup(self._job_id) if self._job_id else None
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramRetryAfter as exc:
            self._retry_until = time.monotonic() + float(exc.retry_after)
            logger.warning("telegram_progress_rate_limited retry_after=%s", exc.retry_after)
        except TelegramBadRequest:
            pass
        except Exception as exc:
            logger.warning("telegram_progress_update_failed error=%s", exc)

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
        try:
            await self._bot.send_document(chat_id=chat_id, document=doc, caption=caption)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(float(exc.retry_after), 5.0))
            await self._bot.send_document(
                chat_id=chat_id,
                document=BufferedInputFile(content, filename=filename),
                caption=caption,
            )


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
    async def cmd_check(message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
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
            uid, outcome, fb_name, _avatar_url = await bulk_service.check_single_uid_profile(
                callback_data.uid
            )
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        text = format_single_check(uid, outcome, fb_name=fb_name)
        markup = build_single_check_markup(uid, outcome)
        with contextlib.suppress(TelegramBadRequest):
            if msg.photo:
                await msg.edit_caption(caption=text, reply_markup=markup)
            else:
                await msg.edit_text(text, reply_markup=markup)
        await query.answer(f"🔄 Kết quả mới nhất: {outcome.value}")

    @router.callback_query(CancelBulkCallback.filter())
    async def cb_cancel_bulk(
        query: CallbackQuery,
        callback_data: CancelBulkCallback,
    ) -> None:
        msg = query.message
        if not isinstance(msg, Message):
            await query.answer()
            return
        cancelled = bulk_service.cancel_bulk_job(msg.chat.id, callback_data.job_id)
        if cancelled:
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_text(
                    f"🛑 Đã dừng phiên kiểm tra hàng loạt (<code>#{callback_data.job_id}</code>).",
                    reply_markup=None,
                )
            await query.answer("🛑 Đã dừng kiểm tra.")
        else:
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_reply_markup(reply_markup=None)
            await query.answer("Phiên kiểm tra đã kết thúc.")

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
        raw_bytes = buf.getvalue()
        if bulk_service.is_chat_busy(message.chat.id):
            await message.answer(
                "⏳ Chat đang có 1 phiên check hàng loạt chạy, vui lòng chờ hoàn tất."
            )
            return
        try:
            if len(raw_bytes) > 65536:
                job_id, uids, _ = await asyncio.to_thread(
                    lambda: bulk_service.prepare_bulk_text(
                        raw_bytes.decode("utf-8", errors="ignore")
                    )
                )
            else:
                job_id, uids, _ = bulk_service.prepare_bulk_text(
                    raw_bytes.decode("utf-8", errors="ignore")
                )
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        await _start_bulk_prepared(message, bulk_service, job_id, uids)

    @router.message(
        StateFilter(None, ListLookupStates.waiting_uid),
        F.text,
        ~F.text.startswith("/"),
    )
    async def handle_smart_plain_text(message: Message, state: FSMContext) -> None:
        """Paste 1 UID/FB link to check immediately, or 2+ UIDs to bulk check."""
        text = (message.text or "").strip()
        if not text:
            return
        single = extract_uid(text)
        if single:
            await _reply_single_check(message, bulk_service, single)
            return
        if "\n" in text:
            try:
                job_id, uids, _ = bulk_service.prepare_bulk_text(text)
            except ValueError:
                return
            if len(uids) == 1:
                await _reply_single_check(message, bulk_service, uids[0])
            elif len(uids) >= 2:
                if await state.get_state() is not None:
                    await state.clear()
                await _start_bulk_prepared(message, bulk_service, job_id, uids)

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
        uid, outcome, fb_name, avatar_url = await bulk_service.check_single_uid_profile(raw)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    text = format_single_check(uid, outcome, fb_name=fb_name)
    markup = build_single_check_markup(uid, outcome)
    if outcome == CheckOutcome.LIVE and avatar_url:
        try:
            await message.answer_photo(photo=avatar_url, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, reply_markup=markup)


async def _start_bulk_prepared(
    message: Message,
    bulk_service: BulkCheckService,
    job_id: str,
    uids: Sequence[str],
) -> None:
    if message.bot is None:
        return
    if bulk_service.is_chat_busy(message.chat.id):
        await message.answer("⏳ Chat đang có 1 phiên check hàng loạt chạy, vui lòng chờ hoàn tất.")
        return

    status_msg = await message.answer(
        format_bulk_progress(
            job_id=job_id,
            done=0,
            total=len(uids),
            live=0,
            die=0,
            error=0,
        ),
        reply_markup=build_bulk_progress_markup(job_id),
    )
    try:
        bulk_service.start_bulk_uids(
            job_id=job_id,
            chat_id=message.chat.id,
            status_message_id=status_msg.message_id,
            uids=uids,
            notifier=TelegramBulkNotifier(message.bot, job_id=job_id),
        )
    except ValueError as exc:
        with contextlib.suppress(TelegramBadRequest):
            await status_msg.delete()
        await message.answer(str(exc))
