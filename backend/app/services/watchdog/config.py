"""Watchdog configuration (env-driven, no hardcoded secrets)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class WatchdogSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Polling
    watchdog_interval_seconds: float = 10.0
    watchdog_host: str = "main-ec2"

    # Recovery budget
    recovery_max_attempts: int = 5
    recovery_window_seconds: int = 600
    recovery_verify_timeout_seconds: float = 30.0
    recovery_verify_interval_seconds: float = 2.0

    # Notification deduplication
    notification_cooldown_seconds: float = 300.0
    # if same failure persists, optional periodic escalation (0 = no periodic resend)
    escalation_interval_seconds: float = 0.0

    # Telegram
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_alert_level: str = "WARNING"
    telegram_timeout_seconds: float = 5.0
    telegram_max_retries: int = 3
    telegram_rate_limit_per_sec: float = 1.0
    telegram_enabled: bool = False

    # Service endpoints (override for tests)
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 4002
    backend_host: str = "127.0.0.1"
    backend_port: int = 8001
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8000
    demo_host: str = "127.0.0.1"
    demo_port: int = 8010
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    database_url: str = "postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # Persistence
    recovery_state_path: str = "/home/tradingapp/storage/state/watchdog_recovery.json"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_enabled and self.telegram_bot_token and self.telegram_chat_id)


def get_watchdog_settings() -> WatchdogSettings:
    return WatchdogSettings()
