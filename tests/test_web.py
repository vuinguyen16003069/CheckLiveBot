from __future__ import annotations

import fakeredis.aioredis
import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from main import build_webhook_app
from src.core.config import Settings


class _BrokenRedis:
    async def ping(self) -> bool:
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_webhook_health_and_secret_token() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings(
        BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        BOT_MODE="webhook",
        WEBHOOK_BASE_URL="https://demo.herokuapp.com",
        WEBHOOK_PATH="/webhook",
        WEBHOOK_SECRET_TOKEN="super-secret-token",
    )
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    app = build_webhook_app(dp=dp, bot=bot, redis=redis, settings=settings)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["redis"] == "ok"

        # Wrong secret token must be rejected
        bad = await client.post("/webhook", json={"update_id": 1})
        assert bad.status == 401

    await bot.session.close()
    await redis.aclose()


@pytest.mark.asyncio
async def test_health_returns_503_when_redis_is_down() -> None:
    settings = Settings(
        BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        BOT_MODE="webhook",
    )
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    app = build_webhook_app(dp=dp, bot=bot, redis=_BrokenRedis(), settings=settings)  # type: ignore[arg-type]

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 503
        body = await resp.json()
        assert body["status"] == "error"

    await bot.session.close()
