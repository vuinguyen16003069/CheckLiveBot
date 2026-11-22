from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from arq import create_pool, cron
from arq.worker import create_worker
from redis.asyncio import Redis

from src.core.checker import CurlCffiFacebookChecker
from src.core.config import Settings, setup_logging
from src.core.middleware import ThrottlingMiddleware
from src.core.redis import ArqJobQueue, RedisCache, build_arq_redis_settings, create_redis_client
from src.modules.bulk.handlers import TelegramBulkNotifier, create_bulk_router
from src.modules.bulk.service import BulkCheckService
from src.modules.watch.handlers import TelegramWatchNotifier, create_watch_router
from src.modules.watch.service import RedisWatchRepository, WatcherScanService, WatchService

logger = structlog.get_logger(__name__)

BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Mở menu chính"),
    BotCommand(command="check", description="Kiểm tra nhanh 1 UID hoặc link FB"),
    BotCommand(command="bulk", description="Check hàng loạt UID / file .txt"),
    BotCommand(command="add", description="Thêm kèo theo dõi DIE → LIVE"),
    BotCommand(command="addlist", description="Thêm nhiều kèo cùng lúc"),
    BotCommand(command="list", description="Xem danh sách kèo (lọc & phân trang)"),
    BotCommand(command="find", description="Tìm kèo theo UID hoặc ghi chú"),
    BotCommand(command="recheck", description="Quét lại ngay các kèo đang DIE"),
    BotCommand(command="stats", description="Thống kê số lượng kèo"),
    BotCommand(command="history", description="Xem lịch sử biến động gần nhất"),
    BotCommand(command="help", description="Hướng dẫn sử dụng"),
]


def install_uvloop_if_available() -> None:
    if sys.platform != "win32":
        with contextlib.suppress(ImportError):
            import uvloop  # type: ignore[import-not-found]

            uvloop.install()


async def process_bulk_check_job(
    ctx: dict[str, Any],
    job_id: str,
    chat_id: int,
    status_message_id: int,
) -> dict[str, Any] | None:
    bulk_service: BulkCheckService = ctx["bulk_service"]
    notifier: TelegramBulkNotifier = ctx["bulk_notifier"]
    summary = await bulk_service.run_bulk_job(job_id, chat_id, status_message_id, notifier)
    return summary.model_dump() if summary else None


async def scan_die_watches_cron(ctx: dict[str, Any]) -> int:
    scanner: WatcherScanService = ctx["watcher_scanner"]
    return await scanner.scan_once()


async def worker_on_startup(ctx: dict[str, Any]) -> None:
    settings: Settings = ctx.get("settings") or Settings()
    setup_logging(settings.log_level, settings.log_format)

    redis: Redis = ctx.get("redis") or create_redis_client(settings.effective_redis_url)
    await redis.ping()  # type: ignore[misc]
    owns_redis = "redis" not in ctx

    bot: Bot = ctx.get("bot") or Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    owns_bot = "bot" not in ctx

    checker: CurlCffiFacebookChecker = ctx.get("checker") or CurlCffiFacebookChecker(
        timeout_seconds=settings.http_timeout_seconds,
        max_retries=settings.max_retries,
        proxies=settings.proxy_urls,
    )
    owns_checker = "checker" not in ctx

    cache = RedisCache(redis, prefix=settings.redis_key_prefix)
    repo = RedisWatchRepository(redis, prefix=settings.redis_key_prefix)
    bulk_service = BulkCheckService(
        checker=checker,
        cache=cache,
        queue=None,
        concurrency=settings.concurrency,
        cache_ttl_seconds=settings.check_cache_ttl_seconds,
        max_bulk_uids=settings.max_bulk_uids,
        progress_interval_seconds=settings.progress_update_interval_seconds,
    )
    watcher_scanner = WatcherScanService(
        repo=repo,
        checker=checker,
        cache=cache,
        notifier=TelegramWatchNotifier(bot),
        concurrency=settings.concurrency,
        cache_ttl_seconds=settings.check_cache_ttl_seconds,
    )

    ctx.update(
        {
            "settings": settings,
            "redis": redis,
            "bot": bot,
            "checker": checker,
            "bulk_service": bulk_service,
            "bulk_notifier": TelegramBulkNotifier(bot),
            "watcher_scanner": watcher_scanner,
            "_owns_redis": owns_redis,
            "_owns_bot": owns_bot,
            "_owns_checker": owns_checker,
        }
    )
    logger.info("arq_worker_started")


async def worker_on_shutdown(ctx: dict[str, Any]) -> None:
    if ctx.get("_owns_checker") and "checker" in ctx:
        await ctx["checker"].close()
    if ctx.get("_owns_bot") and "bot" in ctx:
        await ctx["bot"].session.close()
    if ctx.get("_owns_redis") and "redis" in ctx:
        await ctx["redis"].aclose()
    logger.info("arq_worker_stopped")


class WorkerSettings:
    functions = [process_bulk_check_job]
    cron_jobs = [
        cron(scan_die_watches_cron, second={0, 30}, unique=True),
    ]
    on_startup = worker_on_startup
    on_shutdown = worker_on_shutdown
    redis_settings = build_arq_redis_settings(Settings().effective_redis_url)
    max_jobs = 10
    job_timeout = 3600
    poll_delay = Settings().arq_poll_delay_seconds
    health_check_interval = 3600


def build_dispatcher(
    *,
    settings: Settings,
    redis: Redis,
    watch_service: WatchService,
    bulk_service: BulkCheckService,
) -> Dispatcher:
    cache = RedisCache(redis, prefix=settings.redis_key_prefix)
    storage = RedisStorage(redis=redis)
    dp = Dispatcher(storage=storage)

    throttle = ThrottlingMiddleware(cache, limit_per_minute=settings.user_rate_limit_per_minute)
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)

    dp.include_router(create_watch_router(watch_service, bulk_service=bulk_service))
    dp.include_router(create_bulk_router(bulk_service, watch_service=watch_service))
    return dp


def build_health_app(*, redis: Redis, settings: Settings) -> web.Application:
    app = web.Application()

    async def health_handler(_request: web.Request) -> web.Response:
        try:
            await redis.ping()  # type: ignore[misc]
        except Exception as exc:
            logger.error("health_check_redis_failed", error=str(exc))
            return web.json_response({"status": "error", "redis": "down"}, status=503)
        return web.json_response(
            {
                "status": "ok",
                "redis": "ok",
                "mode": settings.bot_mode,
                "inline_worker": settings.run_inline_worker,
            }
        )

    app.router.add_get("/health", health_handler)
    app.router.add_get("/", health_handler)
    return app


def build_webhook_app(
    *,
    dp: Dispatcher,
    bot: Bot,
    redis: Redis,
    settings: Settings,
) -> web.Application:
    app = build_health_app(redis=redis, settings=settings)
    webhook_path = (
        settings.webhook_path
        if settings.webhook_path.startswith("/")
        else f"/{settings.webhook_path}"
    )
    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret_token,
    )
    handler.register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)
    return app


def _start_inline_worker(
    *,
    settings: Settings,
    redis: Redis,
    bot: Bot,
    checker: CurlCffiFacebookChecker,
) -> tuple[Any, asyncio.Task[Any]]:
    worker_settings: dict[str, Any] = {
        "functions": [process_bulk_check_job],
        "cron_jobs": WorkerSettings.cron_jobs,
        "on_startup": worker_on_startup,
        "on_shutdown": worker_on_shutdown,
        "max_jobs": WorkerSettings.max_jobs,
        "job_timeout": WorkerSettings.job_timeout,
        "poll_delay": settings.arq_poll_delay_seconds,
        "health_check_interval": 3600,
    }
    worker = create_worker(
        worker_settings,
        redis_settings=build_arq_redis_settings(settings.effective_redis_url),
        handle_signals=False,
        ctx={
            "settings": settings,
            "redis": redis,
            "bot": bot,
            "checker": checker,
        },
    )
    task = asyncio.create_task(worker.async_run(), name="inline-arq-worker")
    return worker, task


async def run_bot(settings: Settings) -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required. Copy .env.example to .env and set BOT_TOKEN.")

    redis = create_redis_client(settings.effective_redis_url)
    await redis.ping()  # type: ignore[misc]
    logger.info("redis_connected")

    arq_pool = await create_pool(build_arq_redis_settings(settings.effective_redis_url))
    checker = CurlCffiFacebookChecker(
        timeout_seconds=settings.http_timeout_seconds,
        max_retries=settings.max_retries,
        proxies=settings.proxy_urls,
    )
    cache = RedisCache(redis, prefix=settings.redis_key_prefix)
    repo = RedisWatchRepository(redis, prefix=settings.redis_key_prefix)
    watch_service = WatchService(
        repo=repo,
        checker=checker,
        cache=cache,
        cache_ttl_seconds=settings.check_cache_ttl_seconds,
        max_active_per_chat=settings.max_active_watches_per_chat,
        concurrency=settings.concurrency,
    )
    bulk_service = BulkCheckService(
        checker=checker,
        cache=cache,
        queue=ArqJobQueue(arq_pool),
        concurrency=settings.concurrency,
        cache_ttl_seconds=settings.check_cache_ttl_seconds,
        max_bulk_uids=settings.max_bulk_uids,
        progress_interval_seconds=settings.progress_update_interval_seconds,
    )
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher(
        settings=settings,
        redis=redis,
        watch_service=watch_service,
        bulk_service=bulk_service,
    )

    with contextlib.suppress(Exception):
        await bot.set_my_commands(BOT_COMMANDS)

    inline_worker: Any = None
    worker_task: asyncio.Task[Any] | None = None
    if settings.run_inline_worker:
        inline_worker, worker_task = _start_inline_worker(
            settings=settings,
            redis=redis,
            bot=bot,
            checker=checker,
        )

    try:
        if settings.bot_mode == "webhook":
            webhook_url = settings.full_webhook_url
            await bot.set_webhook(
                url=webhook_url,
                secret_token=settings.webhook_secret_token,
            )
            logger.info("webhook_registered", url=webhook_url)
            app = build_webhook_app(dp=dp, bot=bot, redis=redis, settings=settings)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=settings.host, port=settings.port)
            await site.start()
            logger.info("webhook_server_listening", host=settings.host, port=settings.port)
            try:
                await asyncio.Event().wait()
            finally:
                await runner.cleanup()
        else:
            health_runner: web.AppRunner | None = None
            if os.environ.get("PORT"):
                health_app = build_health_app(redis=redis, settings=settings)
                health_runner = web.AppRunner(health_app)
                await health_runner.setup()
                health_site = web.TCPSite(health_runner, host=settings.host, port=settings.port)
                await health_site.start()
                logger.info(
                    "health_server_listening_for_polling",
                    host=settings.host,
                    port=settings.port,
                )
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                logger.info("polling_started")
                await dp.start_polling(bot)
            finally:
                if health_runner is not None:
                    await health_runner.cleanup()
    finally:
        if inline_worker is not None and worker_task is not None:
            with contextlib.suppress(Exception):
                await inline_worker.close()
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        await checker.close()
        await arq_pool.close()
        await bot.session.close()
        await redis.aclose()
        logger.info("bot_shutdown_complete")
