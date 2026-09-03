"""Tests for application configuration."""

import os
from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


class TestConfig:
    def test_default_values(self) -> None:
        """Settings should have safe local-development defaults."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
            assert settings.app_name == "IBKR Paper Trading System"
            assert settings.environment == "development"
            assert settings.log_level == "INFO"
            assert settings.ibkr_host == "127.0.0.1"
            assert settings.ibkr_port == 4001
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
            assert settings.jwt_algorithm == "HS256"
            assert settings.jwt_access_token_expire_minutes == 480
            assert settings.database_url == (
                "postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading"
            )
            assert settings.margin_whatif_enabled is False
            assert settings.margin_scan_enabled is False
            assert settings.margin_whatif_timeout_sec == 5.0
            assert settings.margin_scan_max_per_sec == 5.0
            assert settings.margin_snapshot_max_age_sec == 300
            assert settings.min_order_notional == Decimal("100")
            assert settings.pair_ratio_tolerance == Decimal("0.5")
            assert settings.pair_min_deployment_pct == Decimal("0")
            assert settings.market_value_utilisation_cap == Decimal("1.0")
            assert settings.market_value_check_enabled is False

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
