"""Run the isolated position demo stream (Redis + SSE). Does not connect to IBKR."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logger import setup_logging
from app.db.session import create_engine_from_settings
from demo_streaming.api import create_demo_app
from demo_streaming.config import get_demo_settings
from demo_streaming.publisher import PositionBridge
from demo_streaming.stream import PositionStream

logger = logging.getLogger(__name__)


async def _watch_shutdown(server: uvicorn.Server, shutdown: asyncio.Event) -> None:
    """Signal SSE loops to exit as soon as uvicorn begins graceful shutdown."""
    while not server.should_exit:
        await asyncio.sleep(0.1)
    shutdown.set()
    logger.info("Demo stream shutdown signalled to SSE clients")


async def _serve() -> None:
    settings = get_demo_settings()
    setup_logging(level="INFO", filename_prefix="demo")
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    shutdown = asyncio.Event()
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    stream = PositionStream(
        redis,
        settings.demo_stream_name,
        stream_maxlen=settings.demo_stream_maxlen,
    )
    try:
        await stream.ping()
        logger.info("Demo Redis connected: url=%s stream=%s", settings.redis_url, settings.demo_stream_name)
    except Exception:
        logger.exception("Cannot reach Redis at %s (need 127.0.0.1:6379)", settings.redis_url)
        raise
    bridge = PositionBridge(
        factory,
        stream,
        poll_interval=settings.demo_poll_interval_ms / 1000.0,
        signal_watch_limit=settings.demo_signal_watch_limit,
        pnl_emit_interval=settings.demo_pnl_emit_interval_ms / 1000.0,
    )
    app = create_demo_app(
        session_factory=factory,
        redis=redis,
        stream_name=settings.demo_stream_name,
        trading_api_url=settings.trading_api_url,
        shutdown=shutdown,
    )
    config = uvicorn.Config(
        app,
        host=settings.demo_stream_host,
        port=settings.demo_stream_port,
        log_level="info",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    poll_task = asyncio.create_task(bridge.run_forever(), name="demo-position-bridge")
    watch_task = asyncio.create_task(_watch_shutdown(server, shutdown), name="demo-shutdown-watch")
    logger.info(
        "Demo stream serving on %s:%d poll=%dms",
        settings.demo_stream_host,
        settings.demo_stream_port,
        settings.demo_poll_interval_ms,
    )
    try:
        await server.serve()
    finally:
        shutdown.set()
        watch_task.cancel()
        poll_task.cancel()
        for task in (watch_task, poll_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await redis.aclose()
        await engine.dispose()
        logger.info("Demo stream shutdown complete")


def run() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    run()
