"""Application entrypoint for the FastAPI backend execution system."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.api.routes.webhooks import router as webhooks_router
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import get_settings
from app.core.logger import setup_logging
from app.db.session import AsyncSessionLocal
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.oms_service import OMSService
from app.services.model_blue.db_allocation import DatabaseCommittedCapitalProvider
from app.services.model_blue.db_trade_book import DatabaseModelBlueTradeBook
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.order_manager import OrderManager
from app.services.pnl import LivePnlService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager controlling application startup and shutdown.

    Initializes TWSClient -> IBKRExecutionAdapter -> OMSService -> OrderManager.
    """
    settings = get_settings()
    setup_logging(level=settings.log_level)

    logger.info("Initializing paper-trading execution components (IBKR Paper TWS target)...")

    client = TWSClient()
    ibkr_adapter = IBKRExecutionAdapter(
        client=client,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        timeout=float(settings.ibkr_connection_timeout),
    )
    oms = OMSService(adapter=ibkr_adapter)

    persistence = ModelBlueExecutionPersistence(AsyncSessionLocal)
    order_manager = OrderManager(
        oms=oms,
        symbol=settings.trading_symbol,
        quantity=settings.order_quantity,
        order_type="MARKET",
        committed_capital_provider=DatabaseCommittedCapitalProvider(AsyncSessionLocal),
        model_blue_trade_book=DatabaseModelBlueTradeBook(AsyncSessionLocal),
        session_factory=AsyncSessionLocal,
        persistence=persistence,
    )
    order_manager._live_pnl = LivePnlService(AsyncSessionLocal, client)
    try:
        await order_manager.hydrate_runtime_from_db()
    except Exception:
        logger.exception("Failed to hydrate Model Blue/RMS runtime state from PostgreSQL.")

    # Establish TWS connection session
    success = client.connect_and_start(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        timeout=float(settings.ibkr_connection_timeout),
    )
    if not success:
        logger.warning("Initial TWS connection attempt unconfirmed; execution adapter will auto-reconnect on active traffic.")
    else:
        try:
            await order_manager.hydrate_live_pnl()
        except Exception:
            logger.exception("Failed to re-subscribe live P&L for open positions.")

    # Store references on application state for dependency lookup
    fastapi_app.state.client = client
    fastapi_app.state.ibkr_adapter = ibkr_adapter
    fastapi_app.state.oms = oms
    fastapi_app.state.order_manager = order_manager

    critical_count = 0
    baskets = getattr(order_manager, "_baskets", None)
    if baskets is not None:
        critical_count = len(getattr(baskets, "_critical", set()) or set())
    logger.info(
        "Startup hydrate summary: processed_signals=%d open_position_keys=%d critical_baskets=%d ibkr=%s:%d",
        len(order_manager._rms_context.processed_signals),
        len(order_manager._rms_context.open_positions),
        critical_count,
        settings.ibkr_host,
        settings.ibkr_port,
    )

    logger.info(
        "Active execution pipeline: IBKRExecutionAdapter & OMSService (TWS %s:%d)",
        settings.ibkr_host,
        settings.ibkr_port,
    )
    logger.info("Paper-trading execution application is ready.")

    yield

    logger.info("Shutting down paper-trading application...")
    client.disconnect_clean()
    logger.info("TWS Client disconnected cleanly. Shutdown complete.")


def create_app() -> FastAPI:
    """Factory function to build and configure the FastAPI app instance."""
    settings = get_settings()

    fastapi_app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="FastAPI gateway for the IBKR paper trading execution engine.",
        lifespan=lifespan,
    )

    # Global Exception Handler
    @fastapi_app.exception_handler(Exception)
    async def global_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled server error processing request: %s", request.url)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Please try again later."},
        )

    # Register Routers
    fastapi_app.include_router(health_router)
    fastapi_app.include_router(webhooks_router, prefix="/api")
    fastapi_app.include_router(api_router, prefix="/api/v1")

    return fastapi_app


app = create_app()
