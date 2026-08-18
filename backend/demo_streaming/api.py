"""Read-only SSE API for the position demo. Does not trade."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from demo_streaming.snapshot import (
    load_baskets,
    load_orders,
    load_position_rows,
    position_leg_payloads,
)
from demo_streaming.stream import PositionStream

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_demo_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    stream_name: str,
) -> FastAPI:
    stream = PositionStream(redis, stream_name)
    app = FastAPI(title="Position demo stream", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict:
        redis_ok = False
        try:
            redis_ok = await stream.ping()
        except Exception:
            logger.exception("Redis ping failed")
        return {
            "status": "ok" if redis_ok else "degraded",
            "redis": redis_ok,
            "stream": stream.stream_name,
            "mode": "read-only",
        }

    @app.get("/demo/positions")
    async def positions() -> JSONResponse:
        now = datetime.now(UTC)
        async with session_factory() as session:
            rows = await load_position_rows(session)
            baskets = await load_baskets(session)
            orders = await load_orders(session)
        payload = []
        for position, account in rows:
            if position.risk_state != "OPEN":
                continue
            key = (position.account_id, position.trade_id)
            payload.extend(
                position_leg_payloads(
                    position,
                    account,
                    baskets.get(key, []),
                    orders.get(key, []),
                    timestamp=now,
                )
            )
        return JSONResponse({"positions": payload, "market_data_status": "UNAVAILABLE"})

    @app.get("/demo/stream")
    async def sse() -> StreamingResponse:
        async def events():
            yield _sse({"event": "hello", "stream": stream.stream_name})
            last_id = "$"
            while True:
                try:
                    entries = await stream.xread(last_id, block_ms=15000, count=20)
                    if not entries:
                        yield ": keepalive\n\n"
                        continue
                    for entry_id, fields in entries:
                        last_id = entry_id
                        yield _sse(fields | {"redis_id": entry_id})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("SSE redis read failed")
                    yield _sse({"event": "stream_error", "market_data_status": "UNAVAILABLE"})
                    await asyncio.sleep(1)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"
