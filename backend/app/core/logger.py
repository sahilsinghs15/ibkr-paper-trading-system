"""Centralized logging configuration for the trading system."""

import logging
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "trading.log"


def setup_logging(level: str = "INFO") -> None:
    """Configure application-wide logging with console and file handlers.

    Call this once at application startup. Individual modules should use
    ``logging.getLogger(__name__)`` to obtain their logger.

    Args:
        level: Root log level (e.g. "DEBUG", "INFO", "WARNING").
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # Console handler — writes to stderr so stdout stays clean for data
    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setFormatter(formatter)

    # File handler — rotating is deferred to production config; a basic
    # FileHandler is sufficient for local development.
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Close and remove existing handlers to avoid resource leaks on repeated calls
    for handler in root_logger.handlers[:]:
        handler.close()
        root_logger.removeHandler(handler)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Quieten noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
