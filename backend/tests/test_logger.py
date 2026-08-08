"""Tests for logging infrastructure."""

import logging

from app.core.logger import setup_logging


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
