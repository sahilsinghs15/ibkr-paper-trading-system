"""Read-only SSE API for the position demo. Does not trade."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from demo_streaming.snapshot import (
    load_baskets,
    load_closed_position_rows,
    load_orders,
    load_position_rows,
    load_signals,
    position_leg_payloads,
)
from demo_streaming.stream import PositionStream

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Vite build output: app/frontend/dist (repo layout: backend/../frontend/dist)
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
SSE_BLOCK_MS = 2000


def _spa_index() -> FileResponse:
    react_index = FRONTEND_DIST / "index.html"
    if react_index.is_file():
        return FileResponse(react_index)
    return FileResponse(STATIC_DIR / "index.html")


def create_demo_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    stream_name: str,
    trading_api_url: str = "http://127.0.0.1:8000",
    shutdown: asyncio.Event | None = None,
) -> FastAPI:
    stream = PositionStream(redis, stream_name)
    stop = shutdown or asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.shutdown = stop
        yield
        stop.set()

    app = FastAPI(
        title="Position demo stream",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    config_base = trading_api_url.rstrip("/") + "/api/v1/config"

    dist_assets = FRONTEND_DIST / "assets"
    if dist_assets.is_dir():
        app.mount("/assets", StaticFiles(directory=dist_assets), name="frontend-assets")

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

    @app.get("/demo/closed-positions")
    async def closed_positions(account_id: int | None = None) -> JSONResponse:
        now = datetime.now(UTC)
        async with session_factory() as session:
            rows = await load_closed_position_rows(session, account_id=account_id)
            baskets = await load_baskets(session)
            orders = await load_orders(session)
        payload = []
        for position, account in rows:
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
        return JSONResponse({"closed_positions": payload})

    @app.get("/demo/signals")
    async def signals(
        limit: int | None = None,
        page: int = 1,
        page_size: int = 100,
        status: str | None = None,
        account_id: int | None = None,
        ibkr_account: str | None = None,
    ) -> JSONResponse:
        async with session_factory() as session:
            payload = await load_signals(
                session,
                limit=limit,
                page=page,
                page_size=page_size,
                status_filter=status,
                account_id=account_id,
                ibkr_account=ibkr_account,
                return_dict=True,
            )
        if isinstance(payload, list):
            return JSONResponse({"signals": payload})
        return JSONResponse(payload)

    @app.get("/demo/market-data-health")
    async def get_market_data_health() -> JSONResponse:
        pnl_svc = getattr(app.state, "live_pnl_service", None)
        if pnl_svc is not None and hasattr(pnl_svc, "get_market_data_health"):
            return JSONResponse(pnl_svc.get_market_data_health())
        return JSONResponse({
            "active_subscriptions": 0,
            "contracts": [],
            "status": "NO_LIVE_PNL_SERVICE"
        })

    @app.get("/demo/stream")
    async def sse(request: Request) -> StreamingResponse:
        async def events():
            logger.info("SSE client connected: stream=%s", stream.stream_name)
            try:
                yield _sse({"event": "hello", "stream": stream.stream_name})
                last_id = "$"
                while not stop.is_set():
                    if await request.is_disconnected():
                        break
                    try:
                        entries = await stream.xread(
                            last_id, block_ms=SSE_BLOCK_MS, count=20
                        )
                        if stop.is_set() or await request.is_disconnected():
                            break
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
            finally:
                logger.info("SSE client disconnected: stream=%s", stream.stream_name)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.api_route(
        "/api/v1/config/{full_path:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    )
    async def proxy_config_api(request: Request, full_path: str) -> Response:
        """Forward config CRUD to the trading API for same-origin dashboard saves."""
        url = f"{config_base}/{full_path}" if full_path else config_base
        if request.url.query:
            url = f"{url}?{request.url.query}"
        body = await request.body()
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "connection")
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                upstream = await client.request(
                    request.method,
                    url,
                    content=body if body else None,
                    headers=headers,
                )
        except httpx.RequestError as exc:
            logger.exception("Config proxy failed: url=%s", url)
            raise HTTPException(status_code=502, detail=f"Trading API unreachable: {exc}") from exc
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    @app.get("/")
    @app.get("/accounts")
    @app.get("/settings")
    @app.get("/account/{path:path}")
    async def index() -> FileResponse:
        return _spa_index()

    @app.get("/favicon.svg")
    async def favicon() -> FileResponse:
        react_icon = FRONTEND_DIST / "favicon.svg"
        if react_icon.is_file():
            return FileResponse(react_icon)
        raise HTTPException(status_code=404)

    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"
