from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from src.core.checker import CurlCffiFacebookChecker
from src.core.config import Settings
from src.core.middleware import ThrottlingMiddleware
from src.core.redis import RedisLock, create_redis_client
from src.modules.bulk.handlers import create_bulk_router
from src.modules.bulk.service import BulkCheckService
from src.modules.watch.handlers import TelegramWatchNotifier, create_watch_router
from src.modules.watch.service import RedisWatchRepository, WatcherScanService, WatchService

logger = structlog.get_logger(__name__)


def install_uvloop_if_available() -> None:
    if sys.platform != "win32":
        with contextlib.suppress(ImportError):
            import uvloop  # type: ignore[import-not-found]

            uvloop.install()


async def run_watcher_loop(
    scanner: WatcherScanService,
    interval_seconds: int,
    stop_event: asyncio.Event,
    *,
    repo: RedisWatchRepository | None = None,
    cleanup_interval_seconds: int = 21600,
) -> None:
    """Dedicated low-latency watcher loop polling DIE watches + 60-day LIVE auto-archive."""
    delay = max(3, interval_seconds)
    cleanup_every = max(60, cleanup_interval_seconds)
    last_cleanup = 0.0
    logger.info("watcher_loop_started", interval_seconds=delay)
    while not stop_event.is_set():
        try:
            await scanner.scan_once()
            now_mono = time.monotonic()
            if repo is not None and (
                last_cleanup == 0.0 or (now_mono - last_cleanup) >= cleanup_every
            ):
                await repo.cleanup_expired()
                last_cleanup = now_mono
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("watcher_scan_error", error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
    logger.info("watcher_loop_stopped")


async def run_bot(settings: Settings) -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required. Copy .env.example to .env and set BOT_TOKEN.")

    redis = create_redis_client(settings.redis_url)
    await redis.ping()
    logger.info("redis_connected")

    checker = CurlCffiFacebookChecker(
        timeout_seconds=settings.http_timeout_seconds,
        max_retries=settings.max_retries,
        proxies=settings.proxy_urls,
        verify_delay_seconds=settings.verify_delay_seconds,
        max_clients=settings.concurrency,
    )
    lock = RedisLock(redis, prefix=settings.redis_key_prefix)
    repo = RedisWatchRepository(
        redis,
        prefix=settings.redis_key_prefix,
        ttl_days=settings.data_ttl_days,
    )
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    watch_service = WatchService(
        repo=repo,
        checker=checker,
        max_active_per_chat=settings.max_active_watches_per_chat,
        concurrency=settings.concurrency,
    )
    bulk_service = BulkCheckService(
        checker=checker,
        concurrency=settings.concurrency,
        max_bulk_uids=settings.max_bulk_uids,
        progress_interval_seconds=settings.progress_update_interval_seconds,
    )
    scanner = WatcherScanService(
        repo=repo,
        checker=checker,
        lock=lock,
        notifier=TelegramWatchNotifier(bot),
        concurrency=settings.concurrency,
    )

    dp = Dispatcher(storage=MemoryStorage())
    throttle = ThrottlingMiddleware(limit_per_minute=settings.user_rate_limit_per_minute)
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)
    dp.include_router(create_watch_router(watch_service))
    dp.include_router(create_bulk_router(bulk_service))

    with contextlib.suppress(Exception):
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Mở bảng điều khiển & bàn phím nhanh"),
                BotCommand(command="check", description="Kiểm tra nhanh 1 UID hoặc link FB"),
                BotCommand(command="add", description="Lên kèo theo dõi DIE → LIVE"),
                BotCommand(command="list", description="Danh sách kèo (chạm UID xem chi tiết)"),
                BotCommand(command="stats", description="Thống kê số lượng kèo"),
                BotCommand(command="help", description="Hướng dẫn sử dụng"),
            ]
        )

    watcher_stop = asyncio.Event()
    watcher_task: asyncio.Task[Any] = asyncio.create_task(
        run_watcher_loop(
            scanner,
            settings.poll_interval_seconds,
            watcher_stop,
            repo=repo,
            cleanup_interval_seconds=settings.cleanup_interval_seconds,
        ),
        name="watcher-loop",
    )

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("polling_started")
        await dp.start_polling(bot)
    finally:
        watcher_stop.set()
        watcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher_task
        await bulk_service.close()
        await checker.close()
        await bot.session.close()
        await redis.aclose()
        logger.info("bot_shutdown_complete")
