"""Demo-stream settings. Isolated from trading execution config besides DATABASE_URL."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class DemoStreamSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading"
    redis_url: str = "redis://127.0.0.1:6379/0"
    demo_stream_host: str = "127.0.0.1"
    demo_stream_port: int = 8010
    demo_poll_interval_ms: int = 2000
    demo_signal_watch_limit: int = 500
    demo_pnl_emit_interval_ms: int = 5000
    demo_stream_maxlen: int = 10000
    demo_stream_name: str = "positions:stream"
    trading_api_url: str = "http://127.0.0.1:8001"


def get_demo_settings() -> DemoStreamSettings:
    return DemoStreamSettings()
