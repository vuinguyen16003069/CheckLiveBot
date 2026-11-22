from __future__ import annotations

import json
import logging
import sys
from typing import Literal

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    bot_mode: Literal["polling", "webhook"] = Field(default="polling", alias="BOT_MODE")
    run_inline_worker: bool = Field(default=True, alias="RUN_INLINE_WORKER")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    webhook_base_url: str | None = Field(default=None, alias="WEBHOOK_BASE_URL")
    webhook_path: str = Field(default="/webhook", alias="WEBHOOK_PATH")
    webhook_secret_token: str | None = Field(default=None, alias="WEBHOOK_SECRET_TOKEN")

    redis_tls_url: str | None = Field(default=None, alias="REDIS_TLS_URL")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    redis_key_prefix: str = Field(default="checklivebot", alias="REDIS_KEY_PREFIX")

    concurrency: int = Field(default=50, ge=1, le=500, alias="CONCURRENCY")
    poll_interval_seconds: int = Field(default=30, ge=5, alias="POLL_INTERVAL_SECONDS")
    arq_poll_delay_seconds: float = Field(default=5.0, ge=0.5, alias="ARQ_POLL_DELAY_SECONDS")
    http_timeout_seconds: float = Field(default=10.0, gt=0.5, alias="HTTP_TIMEOUT_SECONDS")
    max_retries: int = Field(default=3, ge=1, le=10, alias="MAX_RETRIES")
    check_cache_ttl_seconds: int = Field(default=600, ge=0, alias="CHECK_CACHE_TTL_SECONDS")
    proxy_list: str = Field(default="", alias="PROXY_LIST")

    user_rate_limit_per_minute: int = Field(default=30, ge=0, alias="USER_RATE_LIMIT_PER_MINUTE")
    max_active_watches_per_chat: int = Field(default=100, ge=0, alias="MAX_ACTIVE_WATCHES_PER_CHAT")
    max_bulk_uids: int = Field(default=50000, ge=1, alias="MAX_BULK_UIDS")
    progress_update_interval_seconds: float = Field(
        default=2.5,
        ge=0.5,
        alias="PROGRESS_UPDATE_INTERVAL_SECONDS",
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["console", "json"] = Field(default="console", alias="LOG_FORMAT")

    @property
    def effective_redis_url(self) -> str:
        if self.redis_tls_url and self.redis_tls_url.strip():
            return self.redis_tls_url.strip()
        return self.redis_url.strip()

    @property
    def proxy_urls(self) -> list[str]:
        raw = self.proxy_list.strip()
        if not raw:
            return []
        if raw.startswith("["):
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in raw.split(",") if item.strip()]

    @property
    def full_webhook_url(self) -> str:
        if not self.webhook_base_url:
            raise ValueError("WEBHOOK_BASE_URL is required when BOT_MODE=webhook")
        base = self.webhook_base_url.rstrip("/")
        path = self.webhook_path if self.webhook_path.startswith("/") else f"/{self.webhook_path}"
        return f"{base}{path}"


def setup_logging(level: str = "INFO", log_format: Literal["console", "json"] = "console") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)
