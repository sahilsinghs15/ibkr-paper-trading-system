"""Centralized logging configuration for the trading system."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(trace)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# workspace root /home/tradingapp → storage/logs
LOG_DIR = Path(__file__).resolve().parents[4] / "storage" / "logs"

_request_id: ContextVar[str | None] = ContextVar("log_request_id", default=None)
_signal_id: ContextVar[str | None] = ContextVar("log_signal_id", default=None)
_trade_id: ContextVar[str | None] = ContextVar("log_trade_id", default=None)
_account_id: ContextVar[str | None] = ContextVar("log_account_id", default=None)


def _today_log_path(prefix: str = "trading") -> Path:
    """Return today's dated log path for *prefix* (e.g. trading-YYYY-MM-DD.log)."""
    # Local calendar date for daily files (matches TimedRotatingFileHandler when=midnight)
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    return LOG_DIR / f"{prefix}-{date_str}.log"


def current_log_file(prefix: str = "trading") -> Path:
    """Public helper for tests / diagnostics: today's log file path."""
    return _today_log_path(prefix)


class DatedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Midnight rollover that writes directly to ``{prefix}-YYYY-MM-DD.log``.

    Unlike the stock TimedRotatingFileHandler, yesterday's file is already
    correctly named, so rollover only closes the stream and opens the new
    date's file — no rename of the previous file.
    """

    def __init__(self, prefix: str = "trading", encoding: str = "utf-8") -> None:
        self._prefix = prefix
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        filename = str(_today_log_path(prefix))
        super().__init__(
            filename,
            when="midnight",
            interval=1,
            backupCount=0,
            encoding=encoding,
            delay=False,
            utc=False,
        )

    def doRollover(self) -> None:
        """Close current stream and open today's (new) dated file."""
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]

        self.baseFilename = str(_today_log_path(self._prefix))
        if not self.delay:
            self.stream = self._open()

        # Compute next rollover time (stock TimedRotatingFileHandler logic)
        current_time = int(time.time())
        new_rollover_at = self.computeRollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at = new_rollover_at + self.interval
        self.rolloverAt = new_rollover_at


class TraceContextFilter(logging.Filter):
    """Inject ``record.trace`` from ContextVars for correlation."""

    def filter(self, record: logging.LogRecord) -> bool:
        parts: list[str] = []
        req = _request_id.get()
        sig = _signal_id.get()
        trade = _trade_id.get()
        acct = _account_id.get()
        if req:
            parts.append(f"req={req}")
        if sig:
            parts.append(f"signal={sig}")
        if trade:
            parts.append(f"trade={trade}")
        if acct:
            parts.append(f"acct={acct}")
        record.trace = " ".join(parts) if parts else "-"  # type: ignore[attr-defined]
        return True


def bind_log_context(
    *,
    request_id: str | None = None,
    signal_id: str | None = None,
    trade_id: str | None = None,
    account_id: str | None = None,
) -> None:
    """Set correlation fields for subsequent log records in this context."""
    if request_id is not None:
        _request_id.set(request_id)
    if signal_id is not None:
        _signal_id.set(signal_id)
    if trade_id is not None:
        _trade_id.set(trade_id)
    if account_id is not None:
        _account_id.set(account_id)


def get_log_context() -> dict[str, str | None]:
    """Read the current correlation fields."""
    return {
        "request_id": _request_id.get(),
        "signal_id": _signal_id.get(),
        "trade_id": _trade_id.get(),
        "account_id": _account_id.get(),
    }


def clear_log_context() -> None:
    """Reset all correlation ContextVars."""
    _request_id.set(None)
    _signal_id.set(None)
    _trade_id.set(None)
    _account_id.set(None)


@contextmanager
def log_context(
    *,
    request_id: str | None = None,
    signal_id: str | None = None,
    trade_id: str | None = None,
    account_id: str | None = None,
) -> Iterator[None]:
    """Bind correlation fields for a block, then clear them."""
    bind_log_context(
        request_id=request_id,
        signal_id=signal_id,
        trade_id=trade_id,
        account_id=account_id,
    )
    try:
        yield
    finally:
        clear_log_context()


def setup_logging(level: str = "INFO", *, filename_prefix: str = "trading") -> None:
    """Configure application-wide logging with console and daily file handlers.

    Call this once at application startup. Individual modules should use
    ``logging.getLogger(__name__)`` to obtain their logger.

    Args:
        level: Root log level (e.g. "DEBUG", "INFO", "WARNING").
        filename_prefix: File stem before the date
            (``{prefix}-YYYY-MM-DD.log`` under ``storage/logs``).
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    trace_filter = TraceContextFilter()

    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(trace_filter)

    file_handler = DatedTimedRotatingFileHandler(prefix=filename_prefix, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(trace_filter)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    for handler in root_logger.handlers[:]:
        handler.close()
        root_logger.removeHandler(handler)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("ibapi").setLevel(logging.WARNING)
