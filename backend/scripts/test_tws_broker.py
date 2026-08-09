"""Standalone manual smoke test script for IBKRBroker integration.

Run this script to verify live connection, data retrieval, and order lifecycle
from a running TWS Demo or Gateway instance:
    uv run python scripts/test_tws_broker.py
"""

import asyncio
import logging
import sys
from decimal import Decimal

from app.broker.ibkr.ibkr_broker import IBKRBroker
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.models.order import OrderSide

# Configure basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_tws_broker")


async def main() -> None:
    settings = Settings()

    if settings.ibkr_port in (7496, 4001):
        logger.warning("==================================================")
        logger.warning(
            "WARNING: Configured port %d corresponds to a TWS LIVE trading port!",
            settings.ibkr_port,
        )
        logger.warning("This script will place/modify/cancel actual orders.")
        logger.warning(
            "Ensure you are running against the intended paper/demo environment!"
        )
        logger.warning("==================================================")
    logger.info("Initializing TWSClient...")
    client = TWSClient()

    logger.info("Initializing IBKRBroker Adapter...")
    broker = IBKRBroker(client=client, settings=settings)

    logger.info(
        "Connecting to TWS at %s:%d (clientID=%d, timeout=%d)...",
        settings.ibkr_host,
        settings.ibkr_port,
        settings.ibkr_client_id,
        settings.ibkr_connection_timeout,
    )

    try:
        await broker.login()
        logger.info("SUCCESS: Logged in and handshake completed.")
    except Exception as e:  # noqa: BLE001
        logger.error("FAILURE: Failed to log in to TWS: %s", e)
        return

    # Request Account Margin Details
    logger.info("Requesting account summary / margin data...")
    try:
        margin = await broker.get_margin()
        logger.info("SUCCESS: Account Margin Data Retrieved:")
        logger.info("  Equity (NetLiquidation): %s", margin.equity)
        logger.info("  Available Funds:         %s", margin.available_funds)
        logger.info("  Buying Power:            %s", margin.buying_power)
    except Exception as e:  # noqa: BLE001
        logger.error("FAILURE: Failed to retrieve margin information: %s", e)

    # Request Active Positions
    logger.info("Requesting active positions...")
    try:
        positions = await broker.get_positions()
        logger.info("SUCCESS: Active Positions Retrieved (%d found):", len(positions))
        for pos in positions:
            logger.info(
                "  Symbol: %s, Qty: %d, Avg Cost: %s",
                pos.symbol,
                pos.quantity,
                pos.average_price,
            )
    except Exception as e:  # noqa: BLE001
        logger.error("FAILURE: Failed to retrieve positions: %s", e)

    # ── Live Order Lifecycle Smoke Test ──────────────────────────────
    logger.info("Starting live Order Lifecycle test...")
    test_symbol = settings.ibkr_market_data_symbol or "AAPL"

    # 1. Place a LIMIT BUY order far below market price (safe, will not execute)
    limit_price = Decimal("10.00")
    qty = 1
    logger.info(
        "Placing safe test LIMIT BUY order: symbol=%s, qty=%d, price=%s",
        test_symbol,
        qty,
        limit_price,
    )

    try:
        order = await broker.place_order(
            symbol=test_symbol,
            side=OrderSide.BUY,
            quantity=qty,
            order_type="LIMIT",
            price=limit_price,
        )
        logger.info("SUCCESS: Order submitted. Local Order ID: %s", order.order_id)

        # Wait a brief moment for TWS status callbacks
        logger.info("Waiting 3 seconds for initial status callbacks...")
        await asyncio.sleep(3.0)

        book = await broker.get_order_book()
        logger.info("Current order book after placement:")
        for o in book:
            logger.info(
                "  OrderID: %s, Symbol: %s, Side: %s, Status: %s, Qty: %d, Price: %s",
                o.order_id,
                o.symbol,
                o.side.value,
                o.status.value,
                o.quantity,
                o.price,
            )

        # 2. Modify the Order
        logger.info("Modifying the order: increasing quantity to 2...")
        await broker.modify_order(order.order_id, quantity=2)

        # Wait a moment for TWS modifications to process
        logger.info("Waiting 3 seconds for modification callbacks...")
        await asyncio.sleep(3.0)

        book = await broker.get_order_book()
        logger.info("Current order book after modification:")
        for o in book:
            logger.info(
                "  OrderID: %s, Symbol: %s, Qty: %d, Price: %s, Status: %s",
                o.order_id,
                o.symbol,
                o.quantity,
                o.price,
                o.status.value,
            )

        # 3. Cancel the Order
        logger.info("Cancelling the order...")
        await broker.cancel_order(order.order_id)

        # Wait a moment for TWS cancellation confirmation
        logger.info("Waiting 3 seconds for cancellation callbacks...")
        await asyncio.sleep(3.0)

        book = await broker.get_order_book()
        logger.info("Current order book after cancellation:")
        for o in book:
            logger.info(
                "  OrderID: %s, Status: %s",
                o.order_id,
                o.status.value,
            )

    except Exception as e:  # noqa: BLE001
        logger.error("FAILURE: Exception raised during order lifecycle test: %s", e)

    # Disconnect Cleanly
    logger.info("Disconnecting from TWS...")
    try:
        await broker.disconnect()
        logger.info("SUCCESS: Disconnected cleanly.")
    except Exception as e:  # noqa: BLE001
        logger.error("FAILURE: Failed to disconnect: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
