"""Application entrypoint for the FastAPI backend execution system."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.broker.ibkr.gateway_rate_limiter import GatewayRateLimiter
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import get_settings, refuse_testing_flag_on_order_process
from app.core.logger import setup_logging
from app.db.session import AsyncSessionLocal
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.oms_service import OMSService
from app.services.account_margin import AccountMarginService
from app.services.critical_recovery import CriticalRecoveryService
from app.services.margin_rate import MarginRateService
from app.services.margin_scanner import MarginScanner
from app.services.model_blue.db_allocation import DatabaseCommittedCapitalProvider
from app.services.model_blue.db_trade_book import DatabaseModelBlueTradeBook
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.order_manager import OrderManager
from app.services.pnl import LivePnlService
from app.services.position_reconciler import PositionReconciler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager controlling application startup and shutdown.

    Initializes TWSClient -> IBKRExecutionAdapter -> OMSService -> OrderManager.
    """
    refuse_testing_flag_on_order_process()
    settings = get_settings()
    setup_logging(level=settings.log_level)

    logger.info("Initializing execution components (IBKR Gateway target)...")

    client = TWSClient()
    rate_limiter = GatewayRateLimiter(
        max_msg_per_sec=settings.ibkr_gateway_max_msg_per_sec,
        normal_msg_per_sec=settings.ibkr_gateway_normal_msg_per_sec,
        emergency_reserve_per_sec=settings.ibkr_gateway_emergency_reserve_per_sec,
        max_wait_sec=settings.ibkr_gateway_max_wait_sec,
        error100_cooldown_sec=settings.ibkr_gateway_error100_cooldown_sec,
    )
    client.register_rate_limiter(rate_limiter)
    ibkr_adapter = IBKRExecutionAdapter(
        client=client,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        timeout=float(settings.ibkr_connection_timeout),
        rate_limiter=rate_limiter,
    )
    oms = OMSService(adapter=ibkr_adapter)

    persistence = ModelBlueExecutionPersistence(AsyncSessionLocal)
    margin_rate_service = MarginRateService(AsyncSessionLocal)
    account_margin = AccountMarginService(client, rate_limiter=rate_limiter)
    order_manager = OrderManager(
        oms=oms,
        symbol=settings.trading_symbol,
        quantity=settings.order_quantity,
        order_type="MARKET",
        committed_capital_provider=DatabaseCommittedCapitalProvider(AsyncSessionLocal),
        model_blue_trade_book=DatabaseModelBlueTradeBook(AsyncSessionLocal),
        session_factory=AsyncSessionLocal,
        persistence=persistence,
        account_margin=account_margin,
        margin_rate_service=margin_rate_service,
    )
    order_manager._live_pnl = LivePnlService(
        AsyncSessionLocal, client, rate_limiter=rate_limiter
    )
    testing = os.environ.get("TRADINGAPP_TESTING") == "1"
    if not testing:
        try:
            await order_manager.hydrate_runtime_from_db()
        except Exception:
            logger.exception("Failed to hydrate Model Blue/RMS runtime state from PostgreSQL.")
    else:
        try:
            await order_manager.reload_margin_settings()
        except Exception:
            logger.exception("Failed to load margin settings during test lifespan.")

    critical_recovery = CriticalRecoveryService(
        session_factory=AsyncSessionLocal,
        client=client,
        order_manager=order_manager,
    )
    if order_manager._baskets is not None:
        order_manager._baskets.set_recovery_service(critical_recovery)
        critical_recovery.set_coordinator(order_manager._baskets)
        order_manager._baskets.bind_loop(asyncio.get_running_loop())
    fastapi_app.state.critical_recovery = critical_recovery

    # Establish TWS connection session
    success = client.connect_and_start(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        timeout=float(settings.ibkr_connection_timeout),
    )
    if not success:
        logger.warning(
            "Initial TWS connection attempt unconfirmed; orders will wait until the socket is up."
        )
    elif not testing:
        try:
            await order_manager.hydrate_live_pnl()
        except Exception:
            logger.exception("Failed to re-subscribe live P&L for open positions.")
        try:
            account_margin.start()
        except Exception:
            logger.exception("Failed to start AccountMarginService.")

    # Store references on application state for dependency lookup
    fastapi_app.state.session_factory = AsyncSessionLocal
    fastapi_app.state.client = client
    fastapi_app.state.ibkr_adapter = ibkr_adapter
    fastapi_app.state.oms = oms
    fastapi_app.state.order_manager = order_manager
    fastapi_app.state.account_margin = account_margin

    from app.services.recovery import RecoveryManager
    from app.services.worker_pool import ExecutionWorkerPool

    # Run startup recovery scanner
    recovery_mgr = RecoveryManager(AsyncSessionLocal, order_manager)
    if not testing:
        try:
            await recovery_mgr.run_startup_recovery()
        except Exception:
            logger.exception("Failed to execute startup crash recovery scanner.")

    margin_scanner = MarginScanner(
        AsyncSessionLocal,
        ibkr_adapter,
        margin_rate_service,
        live_pnl=order_manager._live_pnl,
    )
    fastapi_app.state.margin_scanner = margin_scanner
    if settings.margin_scan_enabled and success and not testing:
        try:
            await margin_scanner.run_scan(
                budget_sec=float(settings.margin_scan_startup_budget_sec)
            )
            await order_manager.reload_margin_rates()
        except Exception:
            logger.exception("Startup margin scan failed")

    # Start background execution worker pool
    worker_pool = ExecutionWorkerPool(
        session_factory=AsyncSessionLocal,
        order_manager=order_manager,
        worker_count=10,
    )
    if not testing:
        await worker_pool.start()
    fastapi_app.state.worker_pool = worker_pool
    margin_scanner._worker_pool = worker_pool
    if settings.margin_scan_enabled and not testing:
        await margin_scanner.start_background()

    position_reconciler = PositionReconciler(
        AsyncSessionLocal,
        client,
        after_sweep=order_manager.after_reconcile_sweep,
    )
    if not testing:
        await position_reconciler.start()
    fastapi_app.state.position_reconciler = position_reconciler

    if not testing:
        try:
            await critical_recovery.enqueue_all_critical()
        except Exception:
            logger.exception("Failed to enqueue critical baskets at startup.")

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
    if hasattr(fastapi_app.state, "margin_scanner"):
        await fastapi_app.state.margin_scanner.stop()
    if hasattr(fastapi_app.state, "account_margin"):
        fastapi_app.state.account_margin.stop()
    if hasattr(fastapi_app.state, "position_reconciler"):
        await fastapi_app.state.position_reconciler.stop()
    if hasattr(fastapi_app.state, "worker_pool"):
        await fastapi_app.state.worker_pool.stop()
    if hasattr(fastapi_app.state, "critical_recovery"):
        await fastapi_app.state.critical_recovery.stop()
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

    # Register Routers (webhooks live on app.webhook_ingest:app, port 8000)
    fastapi_app.include_router(health_router)
    fastapi_app.include_router(api_router, prefix="/api/v1")

    return fastapi_app


app = create_app()
