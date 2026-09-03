"""Application configuration loaded from environment variables."""

import os
import sys
from decimal import Decimal
from typing import Annotated

from annotated_types import Ge, Gt, Le
from pydantic import AliasChoices, Field
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
        populate_by_name=True,
    )

    # Application
    app_name: str = "IBKR Paper Trading System"
    environment: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = (
        "postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading"
    )

    # JWT & Authentication
    jwt_secret_key: str = (
        "PRODUCTION_JWT_SECRET_KEY_CHANGE_IN_ENV_MUST_BE_SECURE_32_BYTES"
    )
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 480

    # IBKR connection — live Gateway 4001 is the only production port.
    ibkr_host: str = "127.0.0.1"
    ibkr_port: Annotated[int, Gt(0)] = 4001
    ibkr_client_id: Annotated[int, Ge(0)] = 1
    ibkr_connection_timeout: Annotated[int, Gt(0)] = 10

    # IBKR Gateway rate limiter (single-socket production pacing)
    ibkr_gateway_max_msg_per_sec: Annotated[float, Gt(0)] = 30.0
    ibkr_gateway_normal_msg_per_sec: Annotated[float, Gt(0)] = 24.0
    ibkr_gateway_emergency_reserve_per_sec: Annotated[float, Ge(0)] = 6.0
    ibkr_gateway_max_wait_sec: Annotated[float, Gt(0)] = 8.0
    ibkr_gateway_error100_cooldown_sec: Annotated[float, Ge(0)] = 2.0

    # Sizing gates (pair-budget / whole-share rounding)
    min_order_notional: Annotated[Decimal, Gt(0)] = Decimal(100)
    pair_ratio_tolerance: Annotated[Decimal, Gt(0), Le(1)] = Decimal("0.5")
    pair_min_deployment_pct: Annotated[Decimal, Ge(0), Le(1)] = Decimal(0)

    # RMS check 101 — model market-value cap
    # Cap defaults to 1.0 because pair_max_allocation_pct is the finer-grained control.
    market_value_utilisation_cap: Annotated[Decimal, Gt(0), Le(1)] = Decimal("1.0")
    market_value_check_enabled: bool = False

    # What-if plumbing (infra; operator policy lives in margin_settings)
    margin_whatif_enabled: bool = False
    margin_whatif_timeout_sec: Annotated[float, Gt(0)] = 5.0

    # Scanner pacing / scope
    margin_scan_enabled: bool = False
    margin_scan_max_per_sec: Annotated[float, Gt(0)] = 5.0
    margin_scan_startup_budget_sec: Annotated[float, Gt(0)] = 20.0
    margin_scan_probe_notional: Annotated[Decimal, Gt(0)] = Decimal("1000")
    margin_scan_signal_lookback_days: Annotated[int, Gt(0)] = 30

    # Rate table freshness
    margin_rate_max_age_days: Annotated[int, Gt(0)] = 7
    margin_rate_refresh_sec: Annotated[int, Gt(0)] = 300
    margin_snapshot_max_age_sec: Annotated[int, Gt(0)] = 300

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

    # Production Model Blue: TradingView sends STK; submit maps to IBKR CFD.
    # Raw / persisted signal instrument_type stays STK; executed secType is CFD.
    # Legacy env PAPER_EXECUTE_STK_AS_CFD is still accepted.
    execute_stk_as_cfd: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "EXECUTE_STK_AS_CFD",
            "PAPER_EXECUTE_STK_AS_CFD",
            "execute_stk_as_cfd",
            "paper_execute_stk_as_cfd",
        ),
    )

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


def running_under_pytest() -> bool:
    """True when this process is a pytest run (conftest is the only legit testing setter)."""
    return os.environ.get("PYTEST_CURRENT_TEST") is not None or "pytest" in sys.modules


def refuse_testing_flag_on_order_process() -> None:
    """Refuse TRADINGAPP_TESTING=1 on any process that can placeOrder outside pytest."""
    if os.environ.get("TRADINGAPP_TESTING") == "1" and not running_under_pytest():
        raise RuntimeError(
            "TRADINGAPP_TESTING=1 is not allowed on a process that can placeOrder. "
            "pytest/conftest.py is the only legitimate setter."
        )


def assert_webhook_auth_configured(settings: Settings) -> None:
    """Fail closed when webhook auth is enabled but no secret is configured."""
    if settings.webhook_auth_enabled and not settings.webhook_auth_secret:
        raise RuntimeError(
            "WEBHOOK_AUTH_ENABLED=true requires WEBHOOK_AUTH_SECRET. Refusing to start."
        )


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
    if not running_under_pytest():
        assert_webhook_auth_configured(settings)
    return settings
