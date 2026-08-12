"""Application entrypoint for the FastAPI backend."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.api.routes.webhooks import router as webhooks_router
from app.broker.base_broker import BaseBroker
from app.core.config import get_settings
from app.core.logger import setup_logging
from app.market_data.candle_builder import CandleBuilder
from app.services.order_manager import OrderManager
from app.services.trading_service import TradingService
from app.strategy.five_candle_strategy import FiveCandleStrategy

logger = logging.getLogger(__name__)


async def _market_data_consumer(
    app: FastAPI,
) -> None:
    """Background task polling IBKRMarketDataAdapter queue and feeding TradingService.

    Runs only in IBKR mode when a market-data subscription is active.
    """
    from app.market_data.ibkr_market_data import IBKRMarketDataAdapter

    adapter: IBKRMarketDataAdapter | None = getattr(
        app.state, "market_data_adapter", None
    )
    service: TradingService | None = getattr(app.state, "trading_service", None)
    if adapter is None or service is None:
        return

    logger.info("Market data consumer background task started.")
    try:
        while True:
            event = await asyncio.get_running_loop().run_in_executor(
                None, adapter.get_event, 0.5
            )
            if event is not None:
                try:
                    await service.process_market_data(event)
                except Exception:
                    logger.exception("Error processing TWS market data event")
            # Yield control to allow cancellation
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        logger.info("Market data consumer background task stopped.")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Manages application startup and shutdown lifecycles.

    Initializes configuration, logging, broker connections, and
    trading services.  Selects MockBroker or IBKRBroker based on
    the ``broker_mode`` configuration setting.
    """
    settings = get_settings()

    # Configure application logging
    setup_logging(level=settings.log_level)

    broker_mode = settings.broker_mode.lower()
    logger.info(
        "Initializing paper-trading application components (broker_mode=%s)...",
        broker_mode,
    )

    if broker_mode == "ibkr":
        from app.broker.ibkr.ibkr_broker import IBKRBroker
        from app.broker.ibkr.tws_client import TWSClient
        from app.market_data.ibkr_market_data import IBKRMarketDataAdapter

        client = TWSClient()
        broker: BaseBroker = IBKRBroker(client=client, settings=settings)
        market_data_adapter = IBKRMarketDataAdapter(client=client, settings=settings)

        fastapi_app.state.market_data_adapter = market_data_adapter
        logger.info(
            "Active broker: IBKRBroker (TWS %s:%d)",
            settings.ibkr_host,
            settings.ibkr_port,
        )
    else:
        from app.broker.mock_broker import MockBroker

        broker = MockBroker()
        fastapi_app.state.market_data_adapter = None
        logger.info("Active broker: MockBroker")

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

    # Establish broker connection session
    await broker.login()

    # Store references on application state for dependency lookup
    fastapi_app.state.broker = broker
    fastapi_app.state.trading_service = trading_service
    fastapi_app.state.broker_mode = broker_mode

    # Start background market-data consumer (IBKR mode only)
    consumer_task: asyncio.Task[None] | None = None
    if broker_mode == "ibkr":
        consumer_task = asyncio.create_task(_market_data_consumer(fastapi_app))
        fastapi_app.state.market_data_consumer_task = consumer_task

    logger.info("Paper-trading application is ready.")

    yield

    logger.info("Shutting down paper-trading application...")

    # Cancel background market-data consumer
    if consumer_task is not None:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

    # Cancel market data subscription if active
    adapter = getattr(fastapi_app.state, "market_data_adapter", None)
    if adapter is not None:
        try:
            adapter.cancel_market_data()
        except Exception:
            logger.exception("Error cancelling market data subscription")

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

    @fastapi_app.exception_handler(ConnectionError)
    async def connection_error_handler(
        request: Request, exc: ConnectionError
    ) -> JSONResponse:
        logger.error("Broker connection error: %s %s", request.url.path, exc)
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
        )

    # Register API business routes
    fastapi_app.include_router(api_router, prefix="/api/v1")

    # Register Webhook routes
    fastapi_app.include_router(webhooks_router, prefix="/api")

    # Register health endpoints outside version prefix
    fastapi_app.include_router(health_router)

    return fastapi_app


app = create_app()
