from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _load_env_file(path: Path = Path(".env")) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = raw_val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key] = val
    return values


class Settings(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    redis_key_prefix: str = Field(default="checklivebot", alias="REDIS_KEY_PREFIX")

    concurrency: int = Field(default=100, ge=1, le=500, alias="CONCURRENCY")
    poll_interval_seconds: int = Field(default=15, ge=3, alias="POLL_INTERVAL_SECONDS")
    ttl_die_days: int = Field(default=90, ge=1, alias="TTL_DIE_DAYS")
    ttl_live_days: int = Field(default=60, ge=1, alias="TTL_LIVE_DAYS")
    ttl_done_days: int = Field(default=30, ge=1, alias="TTL_DONE_DAYS")
    ttl_cancelled_days: int = Field(default=14, ge=1, alias="TTL_CANCELLED_DAYS")
    ttl_pref_days: int = Field(default=365, ge=1, alias="TTL_PREF_DAYS")
    ttl_seq_days: int = Field(default=0, ge=0, alias="TTL_SEQ_DAYS")
    notify_lock_ttl_seconds: int = Field(default=300, ge=30, alias="NOTIFY_LOCK_TTL_SECONDS")
    http_timeout_seconds: float = Field(default=5.0, gt=0.5, alias="HTTP_TIMEOUT_SECONDS")
    max_retries: int = Field(default=2, ge=1, le=10, alias="MAX_RETRIES")
    verify_delay_seconds: float = Field(default=0.5, ge=0.0, le=5.0, alias="VERIFY_DELAY_SECONDS")
    proxy_list: str = Field(default="", alias="PROXY_LIST")

    user_rate_limit_per_minute: int = Field(default=30, ge=0, alias="USER_RATE_LIMIT_PER_MINUTE")
    max_active_watches_per_chat: int = Field(default=100, ge=0, alias="MAX_ACTIVE_WATCHES_PER_CHAT")
    max_bulk_uids: int = Field(default=50000, ge=1, alias="MAX_BULK_UIDS")
    progress_update_interval_seconds: float = Field(
        default=1.5,
        ge=0.5,
        alias="PROGRESS_UPDATE_INTERVAL_SECONDS",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    log_format: Literal["console", "json"] = Field(default="console", alias="LOG_FORMAT")

    def __init__(self, **overrides: Any) -> None:
        override_keys = {key.lower() for key in overrides}
        merged_env = {**_load_env_file(), **os.environ}
        filtered_env = {k: v for k, v in merged_env.items() if k.lower() not in override_keys}
        super().__init__(**filtered_env, **overrides)

    @property
    def proxy_urls(self) -> list[str]:
        raw = self.proxy_list.strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        base = f"{ts} [{record.levelname:<7}] {record.name}: {record.getMessage()}"
        if record.exc_info:
            return f"{base}\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(level: str = "INFO", log_format: Literal["console", "json"] = "console") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if log_format == "json" else _ConsoleFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)
