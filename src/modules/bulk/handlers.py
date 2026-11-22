from __future__ import annotations

import io
from collections.abc import Sequence

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.core.ports import CheckOutcome
from src.modules.bulk.domain import (
    build_result_files,
    extract_uid,
    extract_uids_from_text,
    format_single_check,
)
from src.modules.bulk.service import BulkCheckService
from src.modules.watch.service import WatchService
from src.modules.watch.ui import QuickWatchCallback, build_cancel_keyboard

logger = structlog.get_logger(__name__)


class BulkStates(StatesGroup):
    waiting_for_uids = State()


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
    ) -> None:
        files = build_result_files(live_uids, die_uids, error_uids)
        if not files:
            await self._bot.send_message(chat_id=chat_id, text=caption)
            return

        for idx, (filename, content) in enumerate(files):
            doc = BufferedInputFile(content, filename=f"{job_id}_{filename}")
            await self._bot.send_document(
                chat_id=chat_id,
                document=doc,
                caption=caption if idx == 0 else None,
            )


def _quick_watch_markup(uid: str, outcome: CheckOutcome) -> InlineKeyboardMarkup | None:
    if outcome == CheckOutcome.LIVE:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Lên kèo UID này",
                    callback_data=QuickWatchCallback(uid=uid).pack(),
                )
            ]
        ]
    )


def create_bulk_router(
    bulk_service: BulkCheckService,
    watch_service: WatchService | None = None,
) -> Router:
    router = Router(name="bulk")

    @router.message(Command("check"))
    async def cmd_check(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "🔎 Cú pháp: <code>/check &lt;UID hoặc link profile.php?id=...&gt;</code>\n"
                "💡 Mẹo: Bạn cũng có thể dán thẳng UID vào chat mà không cần gõ /check!"
            )
            return
        await _reply_single_check(message, bulk_service, raw)

    @router.message(Command("bulk"))
    async def cmd_bulk(message: Message, command: CommandObject, state: FSMContext) -> None:
        inline_text = (command.args or "").strip()
        if inline_text:
            await _enqueue_text(message, bulk_service, inline_text)
            return
        await state.set_state(BulkStates.waiting_for_uids)
        await message.answer(
            "📦 <b>Chế độ Check hàng loạt (ARQ Worker)</b>\n\n"
            "Gửi danh sách UID (mỗi dòng 1 UID) hoặc gửi trực tiếp file <code>.txt</code>.",
            reply_markup=build_cancel_keyboard(),
        )

    @router.message(BulkStates.waiting_for_uids, F.text)
    async def handle_bulk_text(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if text.lower() == "/cancel":
            await state.clear()
            await message.answer("🛑 Đã hủy chế độ kiểm tra hàng loạt.")
            return
        await state.clear()
        await _enqueue_text(message, bulk_service, text)

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

        current_state = await state.get_state()
        if current_state == BulkStates.waiting_for_uids.state:
            await state.clear()

        buf = io.BytesIO()
        await bot.download(document, destination=buf)
        text = buf.getvalue().decode("utf-8", errors="ignore")

        # Smart detect: if lines look like watch rows ("UID | note | price"), import as watches
        non_empty = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if (
            watch_service is not None
            and non_empty
            and all("|" in ln for ln in non_empty[:5])
            and current_state != BulkStates.waiting_for_uids.state
        ):
            user_id = message.from_user.id if message.from_user else 0
            added, errors = await watch_service.add_watch_batch(message.chat.id, user_id, text)
            lines = [f"✅ Nhận diện file danh sách kèo: đã thêm/cập nhật <b>{len(added)}</b> kèo."]
            if errors:
                lines.append(f"⚠️ Có <b>{len(errors)}</b> dòng lỗi:")
                lines.extend(errors[:10])
            await message.answer("\n".join(lines))
            return

        await _enqueue_text(message, bulk_service, text)

    @router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
    async def handle_smart_plain_text(message: Message) -> None:
        """Smart UX: paste 1 UID/FB link to check immediately, or 2+ UIDs to bulk check."""
        text = (message.text or "").strip()
        if not text:
            return
        single = extract_uid(text)
        if single:
            await _reply_single_check(message, bulk_service, single)
            return
        if "\n" in text:
            uids, _ = extract_uids_from_text(text, max_uids=50000)
            if len(uids) >= 2:
                await _enqueue_text(message, bulk_service, text)

    return router


async def _reply_single_check(
    message: Message,
    bulk_service: BulkCheckService,
    raw: str,
) -> None:
    try:
        uid, outcome, from_cache = await bulk_service.check_single_uid(raw)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    await message.answer(
        format_single_check(uid, outcome, from_cache),
        reply_markup=_quick_watch_markup(uid, outcome),
    )


async def _enqueue_text(message: Message, bulk_service: BulkCheckService, text: str) -> None:
    status_msg = await message.answer("⏳ Đang tiếp nhận và đẩy danh sách UID vào hàng đợi ARQ...")
    try:
        job_id, total, skipped = await bulk_service.enqueue_bulk_text(
            chat_id=message.chat.id,
            text=text,
            status_message_id=status_msg.message_id,
        )
    except ValueError as exc:
        await status_msg.edit_text(f"❌ {exc}")
        return
    except RuntimeError as exc:
        await status_msg.edit_text(f"⚠️ {exc}")
        return

    extra = f" (đã lọc bỏ {skipped:,} dòng trùng/lỗi)" if skipped else ""
    await status_msg.edit_text(
        f"📥 <b>Đã nhận {total:,} UID</b>{extra}\n"
        f"🆔 Job: <code>#{job_id}</code>\n"
        "⚙️ Worker đang xử lý ngầm và sẽ cập nhật tiến độ mỗi vài giây..."
    )
