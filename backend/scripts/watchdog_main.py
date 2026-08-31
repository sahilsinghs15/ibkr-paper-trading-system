#!/usr/bin/env python3
"""Entrypoint for watchdog daemon — handles SIGTERM/SIGINT gracefully."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parents[1]
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.core.logger import setup_logging
from app.services.watchdog.config import get_watchdog_settings
from app.services.watchdog.daemon import WatchdogDaemon

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_watchdog_settings()
    setup_logging(level="INFO", filename_prefix="watchdog")
    logging.getLogger("watchdog").info("Starting watchdog host=%s interval=%.1fs", settings.watchdog_host, settings.watchdog_interval_seconds)
    daemon = WatchdogDaemon(settings)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        logger.info("Watchdog received signal %s — graceful STOP", signum)
        daemon._stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        loop.run_until_complete(daemon.start())
        # block until STOP
        loop.run_until_complete(daemon._stop.wait())
    except KeyboardInterrupt:
        pass
    finally:
        # graceful shutdown — STOP not FAILURE
        try:
            loop.run_until_complete(daemon.stop())
        except Exception:
            pass
        loop.close()
        logger.info("Watchdog stopped gracefully — STOP (not FAILURE)")


if __name__ == "__main__":
    main()
