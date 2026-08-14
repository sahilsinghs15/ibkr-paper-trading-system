#!/usr/bin/env sys
"""Smoke test script verifying Python-to-TWS connection handshake and disconnect."""

import logging
import sys

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import get_settings
from app.core.logger import setup_logging

# Configure logging to stdout
setup_logging(level="INFO")
logger = logging.getLogger("test_tws_connection")


def main() -> None:
    """Run the TWS connection smoke test."""
    settings = get_settings()

    logger.info("Initializing TWSClient...")
    client = TWSClient()

    # Attempt connection
    success = client.connect_and_start(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        timeout=float(settings.ibkr_connection_timeout),
    )

    if success:
        logger.info("SUCCESS: Connected to TWS Demo successfully!")
        logger.info("Next Valid Order ID: %s", client.next_order_id)
        # Disconnect cleanly
        client.disconnect_clean()
        sys.exit(0)
    else:
        logger.error("FAILURE: Could not connect to TWS Demo or handshake timed out.")
        sys.exit(1)


if __name__ == "__main__":
    main()
