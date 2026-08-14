#!/usr/bin/env sys
"""Smoke test script verifying Python-to-TWS live/delayed market data feed."""

import logging
import sys
import time

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import get_settings
from app.core.logger import setup_logging
from app.market_data.ibkr_market_data import IBKRMarketDataAdapter

# Configure logging to stdout
setup_logging(level="INFO")
logger = logging.getLogger("test_tws_market_data")


def main() -> None:
    """Run TWS market data smoke test."""
    settings = get_settings()

    logger.info("Initializing TWSClient...")
    client = TWSClient()

    # Connect to TWS Demo
    connected = client.connect_and_start(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        timeout=float(settings.ibkr_connection_timeout),
    )

    if not connected:
        logger.error("FAILURE: Could not connect to TWS Demo or handshake timed out.")
        sys.exit(1)

    logger.info("Initializing IBKRMarketDataAdapter...")
    adapter = IBKRMarketDataAdapter(client, settings)

    try:
        req_id = adapter.request_market_data()
        logger.info("Active subscription reqId: %d", req_id)

        # Wait for a small number of events (e.g. 3 events)
        logger.info("Waiting for up to 3 market-data events (timeout 15 seconds)...")
        events_received = 0
        max_wait_seconds = 15
        start_time = time.time()

        while events_received < 3 and (time.time() - start_time) < max_wait_seconds:
            event = adapter.get_event(timeout=1.0)
            if event is not None:
                events_received += 1
                logger.info(
                    "Normalized Event #%d: Price=%s, Volume=%d, Time=%s",
                    events_received,
                    event.price,
                    event.volume,
                    event.timestamp,
                )

        if events_received > 0:
            logger.info(
                "SUCCESS: Successfully received and normalized %d market-data events!",
                events_received,
            )
            exit_code = 0
        else:
            logger.error(
                "FAILURE: Received 0 market-data events. This is likely because the "
                "TWS Demo environment does not have a subscription to the requested "
                "market data (delayed or live) for symbol '%s' on exchange '%s'.",
                settings.ibkr_market_data_symbol,
                settings.ibkr_market_data_exchange,
            )
            exit_code = 1

    except Exception as e:  # noqa: BLE001
        logger.error("Unexpected error during market-data test: %s", e)
        exit_code = 1
    finally:
        logger.info("Cleaning up...")
        adapter.cancel_market_data()
        client.disconnect_clean()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
