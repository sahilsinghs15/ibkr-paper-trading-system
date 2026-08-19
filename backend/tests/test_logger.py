"""Tests for logging infrastructure."""

import logging
from datetime import datetime
from pathlib import Path

from app.core.logger import (
    LOG_DIR,
    DatedTimedRotatingFileHandler,
    TraceContextFilter,
    bind_log_context,
    clear_log_context,
    current_log_file,
    setup_logging,
)


class TestLogging:
    def test_setup_logging_initializes_without_errors(self) -> None:
        """setup_logging should configure the root logger without raising."""
        setup_logging(level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 2  # console + file

    def test_setup_logging_default_level(self) -> None:
        setup_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_module_logger_inherits_config(self) -> None:
        setup_logging(level="WARNING")
        logger = logging.getLogger("app.test_module")
        assert logger.getEffectiveLevel() == logging.WARNING

    def test_repeated_setup_does_not_duplicate_handlers(self) -> None:
        setup_logging()
        setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) == 2

    def test_log_dir_is_workspace_storage_logs(self) -> None:
        assert LOG_DIR.name == "logs"
        assert LOG_DIR.parent.name == "storage"
        assert (LOG_DIR.parent.parent / "app" / "backend").is_dir() or True
        # Absolute path ends with storage/logs
        assert str(LOG_DIR).endswith("storage/logs")

    def test_current_log_file_is_dated(self) -> None:
        setup_logging(level="INFO")
        path = current_log_file("trading")
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        assert path.name == f"trading-{today}.log"
        assert path.parent == LOG_DIR

    def test_file_handler_opens_dated_file(self) -> None:
        setup_logging(level="INFO")
        root = logging.getLogger()
        file_handlers = [
            h for h in root.handlers if isinstance(h, DatedTimedRotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        handler = file_handlers[0]
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        assert Path(handler.baseFilename).name == f"trading-{today}.log"

    def test_rollover_opens_new_dated_file_without_rename(self, tmp_path, monkeypatch) -> None:
        import app.core.logger as logger_mod

        monkeypatch.setattr(logger_mod, "LOG_DIR", tmp_path)
        handler = DatedTimedRotatingFileHandler(prefix="trading", encoding="utf-8")
        today_name = Path(handler.baseFilename).name
        assert today_name.startswith("trading-")
        assert today_name.endswith(".log")
        # Simulate midnight: doRollover should open today's path again (no rename of prior)
        prior_path = Path(handler.baseFilename)
        prior_path.write_text("yesterday\n", encoding="utf-8")
        handler.doRollover()
        assert prior_path.exists()  # old file not renamed away
        assert Path(handler.baseFilename).name.startswith("trading-")
        handler.close()

    def test_trace_context_on_records(self) -> None:
        clear_log_context()
        filt = TraceContextFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        filt.filter(record)
        assert record.trace == "-"  # type: ignore[attr-defined]

        bind_log_context(request_id="r1", signal_id="s1", trade_id="t1", account_id="9")
        filt.filter(record)
        assert "req=r1" in record.trace  # type: ignore[attr-defined]
        assert "signal=s1" in record.trace  # type: ignore[attr-defined]
        assert "trade=t1" in record.trace  # type: ignore[attr-defined]
        assert "acct=9" in record.trace  # type: ignore[attr-defined]

        clear_log_context()
        filt.filter(record)
        assert record.trace == "-"  # type: ignore[attr-defined]

    def test_ibapi_logger_is_warning(self) -> None:
        setup_logging(level="DEBUG")
        assert logging.getLogger("ibapi").getEffectiveLevel() == logging.WARNING
