"""Application entrypoint for the FastAPI backend."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.broker.mock_broker import MockBroker
from app.core.config import get_settings
from app.core.logger import setup_logging
from app.market_data.candle_builder import CandleBuilder
from app.services.order_manager import OrderManager
from app.services.trading_service import TradingService
from app.strategy.five_candle_strategy import FiveCandleStrategy

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Manages application startup and shutdown lifecycles.

    Initializes configuration, logging, broker connections, and
    trading services.
    """
    settings = get_settings()

    # Configure application logging
    setup_logging(level=settings.log_level)

    logger.info("Initializing paper-trading application components...")

    # Instantiate broker (uses MockBroker for Phase 2.6)
    broker = MockBroker()

    # Core components instantiation (Dependency Injection root)
    candle_builder = CandleBuilder(timeframe_minutes=settings.candle_timeframe_minutes)
    strategy = FiveCandleStrategy()
    order_manager = OrderManager(
        broker=broker,
        symbol=settings.trading_symbol,
        quantity=settings.order_quantity,
        order_type="MARKET",
    )
    trading_service = TradingService(
        candle_builder=candle_builder,
        strategy=strategy,
        order_manager=order_manager,
    )

    # Establish mock broker connection session
    await broker.login()

    # Store references on application state for dependency lookup
    fastapi_app.state.broker = broker
    fastapi_app.state.trading_service = trading_service

    logger.info("Paper-trading application is ready.")

    yield

    logger.info("Shutting down paper-trading application...")
    await broker.disconnect()
    logger.info("Broker disconnected. Shutdown complete.")


def create_app() -> FastAPI:
    """Factory function to build and configure the FastAPI app instance."""
    settings = get_settings()

    fastapi_app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="FastAPI gateway for the FiveCandleStrategy paper trading engine.",
        lifespan=lifespan,
    )

    # Global Exception Handlers
    @fastapi_app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        logger.warning(
            "Client requested invalid operation: %s %s", request.url.path, exc
        )
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc)},
        )

    # Register API business routes
    fastapi_app.include_router(api_router, prefix="/api/v1")

    # Register health endpoints outside version prefix
    fastapi_app.include_router(health_router)

    return fastapi_app


app = create_app()
