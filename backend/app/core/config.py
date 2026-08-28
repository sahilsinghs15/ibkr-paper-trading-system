"""Application configuration loaded from environment variables."""

import os
from decimal import Decimal
from typing import Annotated

from annotated_types import Ge, Gt, Le
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Application settings with defaults safe for local development.

    Values are loaded from environment variables or a .env file.
    All field names map to uppercase environment variable names
    (e.g., app_name -> APP_NAME).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "IBKR Paper Trading System"
    environment: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = (
        "postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading"
    )

    # IBKR connection
    ibkr_host: str = "127.0.0.1"
    ibkr_port: Annotated[int, Gt(0)] = 7497
    ibkr_client_id: Annotated[int, Ge(0)] = 1
    ibkr_connection_timeout: Annotated[int, Gt(0)] = 10

    # IBKR Gateway rate limiter (single-socket production pacing)
    ibkr_gateway_max_msg_per_sec: Annotated[float, Gt(0)] = 30.0
    ibkr_gateway_normal_msg_per_sec: Annotated[float, Gt(0)] = 24.0
    ibkr_gateway_emergency_reserve_per_sec: Annotated[float, Ge(0)] = 6.0
    ibkr_gateway_max_wait_sec: Annotated[float, Gt(0)] = 8.0
    ibkr_gateway_error100_cooldown_sec: Annotated[float, Ge(0)] = 2.0

    # IBKR Market Data connection settings
    ibkr_market_data_type: Annotated[int, Ge(1), Le(4)] = 3
    ibkr_market_data_symbol: str = "AAPL"
    ibkr_market_data_sec_type: str = "STK"
    ibkr_market_data_exchange: str = "SMART"
    ibkr_market_data_currency: str = "USD"
    ibkr_market_data_primary_exchange: str | None = None

    # Trading
    trading_symbol: str = "RELIANCE"
    candle_timeframe: str = "5 mins"
    strategy_candle_count: Annotated[int, Gt(0)] = 5
    order_quantity: Annotated[int, Gt(0)] = 1

    # TEMPORARY paper-testing Model Blue base-leg committed notional (USD).
    # Not live-account allocation and not a production financial default.
    # Unset/None => Model Blue OPEN is rejected (no invented size).
    # Replace later with database/OEMS CommittedCapitalProvider.
    # Env: MODEL_BLUE_COMMITTED_NOTIONAL
    model_blue_committed_notional: Decimal | None = None

    # TEMPORARY paper/client-demo: requested STK executes as IBKR CFD.
    # Raw TradingView / persisted signal instrument_type stays STK.
    # Disable with PAPER_EXECUTE_STK_AS_CFD=false.
    paper_execute_stk_as_cfd: bool = True

    # Webhook Security Settings
    webhook_auth_secret: str | None = None
    webhook_auth_enabled: bool = True

    # Emergency Kill Switch Security Settings
    emergency_killswitch_auth_secret: str | None = None
    emergency_killswitch_auth_enabled: bool = True



    @property
    def candle_timeframe_minutes(self) -> int:
        """Parse candle_timeframe string to get the timeframe in minutes."""
        if self.candle_timeframe == "5 mins":
            return 5
        if self.candle_timeframe.endswith(" mins"):
            try:
                return int(self.candle_timeframe.split()[0])
            except ValueError:
                pass
        return 5


_PRODUCTION_DATABASE_NAME = "ibkr_trading"


def get_settings() -> Settings:
    """Create and return a Settings instance.

    Use this function instead of constructing Settings directly so
    that the creation point is easy to find and override in tests.
    """
    settings = Settings()
    if os.environ.get("TRADINGAPP_TESTING") == "1":
        db_name = make_url(settings.database_url).database
        if db_name == _PRODUCTION_DATABASE_NAME:
            raise RuntimeError(
                "Refusing production database 'ibkr_trading' while TRADINGAPP_TESTING=1. "
                "Use ibkr_trading_test (conftest rewrites DATABASE_URL automatically)."
            )
    return settings
