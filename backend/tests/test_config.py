"""Tests for application configuration."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


class TestConfig:
    def test_default_values(self) -> None:
        """Settings should have safe local-development defaults."""
        settings = Settings(_env_file=None)
        assert settings.app_name == "IBKR Paper Trading System"
        assert settings.environment == "development"
        assert settings.log_level == "INFO"
        assert settings.ibkr_host == "127.0.0.1"
        assert settings.ibkr_port == 7497
        assert settings.ibkr_client_id == 1
        assert settings.ibkr_connection_timeout == 10
        assert settings.ibkr_market_data_type == 3
        assert settings.ibkr_market_data_symbol == "AAPL"
        assert settings.ibkr_market_data_sec_type == "STK"
        assert settings.ibkr_market_data_exchange == "SMART"
        assert settings.ibkr_market_data_currency == "USD"
        assert settings.ibkr_market_data_primary_exchange is None
        assert settings.candle_timeframe == "5 mins"
        assert settings.strategy_candle_count == 5
        assert settings.order_quantity == 1
        assert settings.model_blue_committed_notional is None
        assert settings.database_url == (
            "postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading"
        )

    def test_environment_override(self) -> None:
        """Environment variables should override defaults."""
        overrides = {
            "APP_NAME": "Custom App",
            "ENVIRONMENT": "production",
            "LOG_LEVEL": "DEBUG",
            "IBKR_PORT": "4002",
            "ORDER_QUANTITY": "50",
        }
        with patch.dict(os.environ, overrides, clear=False):
            settings = Settings()
            assert settings.app_name == "Custom App"
            assert settings.environment == "production"
            assert settings.log_level == "DEBUG"
            assert settings.ibkr_port == 4002
            assert settings.order_quantity == 50

    def test_get_settings_returns_instance(self) -> None:
        settings = get_settings()
        assert isinstance(settings, Settings)

    def test_ibkr_port_must_be_positive(self) -> None:
        with (
            patch.dict(os.environ, {"IBKR_PORT": "0"}, clear=False),
            pytest.raises(ValidationError),
        ):
            Settings()

    def test_ibkr_client_id_allows_zero(self) -> None:
        with patch.dict(os.environ, {"IBKR_CLIENT_ID": "0"}, clear=False):
            settings = Settings()
            assert settings.ibkr_client_id == 0

    def test_ibkr_client_id_rejects_negative(self) -> None:
        with (
            patch.dict(os.environ, {"IBKR_CLIENT_ID": "-1"}, clear=False),
            pytest.raises(ValidationError),
        ):
            Settings()

    def test_strategy_candle_count_must_be_positive(self) -> None:
        with (
            patch.dict(os.environ, {"STRATEGY_CANDLE_COUNT": "0"}, clear=False),
            pytest.raises(ValidationError),
        ):
            Settings()

    def test_order_quantity_must_be_positive(self) -> None:
        with (
            patch.dict(os.environ, {"ORDER_QUANTITY": "-5"}, clear=False),
            pytest.raises(ValidationError),
        ):
            Settings()
