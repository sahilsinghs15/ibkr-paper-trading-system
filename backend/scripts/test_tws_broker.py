"""Standalone manual smoke test script for read-only IBKRBroker integration.

Run this script to verify live connection and data retrieval from a running TWS Demo or Gateway instance:
    uv run python scripts/test_tws_broker.py
"""

import asyncio
import logging
import sys

from app.broker.ibkr.ibkr_broker import IBKRBroker
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings

# Configure basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_tws_broker")


async def main() -> None:
    settings = Settings()
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

    # Disconnect Cleanly
    logger.info("Disconnecting from TWS...")
    try:
        await broker.disconnect()
        logger.info("SUCCESS: Disconnected cleanly.")
    except Exception as e:  # noqa: BLE001
        logger.error("FAILURE: Failed to disconnect: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
