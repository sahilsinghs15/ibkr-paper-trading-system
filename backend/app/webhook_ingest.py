"""Standalone FastAPI entrypoint for TradingView webhook ingestion.

This process only authenticates, validates, and durably enqueues signal_jobs
into PostgreSQL. It does not connect to IBKR or run execution workers.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes.health import router as health_router
from app.api.routes.webhooks import router as webhooks_router
from app.core.config import assert_webhook_auth_configured, get_settings
from app.core.logger import setup_logging
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Initialize Postgres session factory only — no IBKR or worker pool."""
    settings = get_settings()
    assert_webhook_auth_configured(settings)
    setup_logging(level=settings.log_level, filename_prefix="webhook")

    fastapi_app.state.session_factory = AsyncSessionLocal
    logger.info("Webhook ingest service ready (Postgres-only, no IBKR).")

    yield

    logger.info("Webhook ingest service shutting down.")


def create_ingest_app() -> FastAPI:
    """Factory for the webhook ingest FastAPI application."""
    settings = get_settings()

    fastapi_app = FastAPI(
        title=f"{settings.app_name} — Webhook Ingest",
        version="0.1.0",
        description="Durable TradingView webhook ingestion into PostgreSQL.",
        lifespan=lifespan,
    )

    @fastapi_app.exception_handler(Exception)
    async def global_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled server error processing request: %s", request.url)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Please try again later."},
        )

    fastapi_app.include_router(health_router)
    fastapi_app.include_router(webhooks_router, prefix="/api")

    return fastapi_app


app = create_ingest_app()
