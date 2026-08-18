"""Run the isolated position demo stream (Redis + SSE). Does not connect to IBKR."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import create_engine_from_settings
from demo_streaming.api import create_demo_app
from demo_streaming.config import get_demo_settings
from demo_streaming.publisher import PositionBridge
from demo_streaming.stream import PositionStream

logger = logging.getLogger(__name__)


async def _serve() -> None:
    settings = get_demo_settings()
    logging.basicConfig(level=logging.INFO)
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_timeout=None,
        socket_connect_timeout=5,
    )
    stream = PositionStream(redis, settings.demo_stream_name)
    try:
        await stream.ping()
    except Exception:
        logger.exception("Cannot reach Redis at %s (need 127.0.0.1:6379)", settings.redis_url)
        raise
    bridge = PositionBridge(
        factory,
        stream,
        poll_interval=settings.demo_poll_interval_ms / 1000.0,
    )
    app = create_demo_app(
        session_factory=factory,
        redis=redis,
        stream_name=settings.demo_stream_name,
    )
    config = uvicorn.Config(
        app,
        host=settings.demo_stream_host,
        port=settings.demo_stream_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    poll_task = asyncio.create_task(bridge.run_forever(), name="demo-position-bridge")
    try:
        await server.serve()
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        await redis.aclose()
        await engine.dispose()


def run() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    run()
