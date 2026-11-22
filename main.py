from __future__ import annotations

import asyncio
import contextlib

from src.core.config import Settings, setup_logging
from src.core.runtime import (
    WorkerSettings,
    build_webhook_app,
    install_uvloop_if_available,
    run_bot,
)

__all__ = ["WorkerSettings", "build_webhook_app", "main"]


def main() -> None:
    install_uvloop_if_available()
    settings = Settings()
    setup_logging(settings.log_level, settings.log_format)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
