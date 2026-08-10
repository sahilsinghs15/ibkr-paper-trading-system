"""Application configuration loaded from environment variables."""

from typing import Annotated

from annotated_types import Ge, Gt, Le
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    )

    # Application
    app_name: str = "IBKR Paper Trading System"
    environment: str = "development"
    log_level: str = "INFO"
    broker_mode: str = "mock"  # "mock" or "ibkr"

    # IBKR connection
    ibkr_host: str = "127.0.0.1"
    ibkr_port: Annotated[int, Gt(0)] = 7497
    ibkr_client_id: Annotated[int, Ge(0)] = 1
    ibkr_connection_timeout: Annotated[int, Gt(0)] = 10

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


def get_settings() -> Settings:
    """Create and return a Settings instance.

    Use this function instead of constructing Settings directly so
    that the creation point is easy to find and override in tests.
    """
    return Settings()
