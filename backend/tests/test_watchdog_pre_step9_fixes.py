"""Tests for Phase 2A pre-Step-9 fixes: postgres port from DATABASE_URL + httpx logging."""

import logging

from app.core.logger import setup_logging
from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.health import PostgresHealthChecker, _postgres_host_port


def test_postgres_port_derived_from_database_url():
    # default database_url has 5433, postgres_port default 5432 — checker must use 5433
    s = WatchdogSettings(database_url="postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading", postgres_port=5432, postgres_host="127.0.0.1")
    host, port = _postgres_host_port(s)
    assert port == 5433
    assert host == "localhost"

    # when DATABASE_URL has different port, that wins
    s2 = WatchdogSettings(database_url="postgresql+asyncpg://user:pass@db.example.com:5439/db", postgres_port=5432)
    host2, port2 = _postgres_host_port(s2)
    assert port2 == 5439
    assert host2 == "db.example.com"

    # fallback when DATABASE_URL malformed
    s3 = WatchdogSettings(database_url="not-a-url", postgres_port=5432, postgres_host="10.0.0.1")
    host3, port3 = _postgres_host_port(s3)
    assert port3 == 5432
    assert host3 == "10.0.0.1"


def test_postgres_checker_uses_database_url_port(monkeypatch):
    # checker should use derived port, not the separate postgres_port field
    s = WatchdogSettings(database_url="postgresql+asyncpg://root:root123@127.0.0.1:5999/ibkr_trading", postgres_port=5432)
    checker = PostgresHealthChecker(s)
    # monkeypatch _tcp_open_async to capture host/port
    captured = {}

    async def fake_tcp(host, port, timeout=1.0):
        captured["host"] = host
        captured["port"] = port
        return False

    monkeypatch.setattr("app.services.watchdog.health._tcp_open_async", fake_tcp)

    import asyncio
    hr = asyncio.run(checker.check())
    assert captured["port"] == 5999
    assert hr.port == 5999
    assert hr.reason == "tcp_refused"


def test_httpx_logging_is_warning_after_setup():
    setup_logging(level="INFO", filename_prefix="test_watchdog_logging")
    # after setup, httpx and httpcore must be WARNING to avoid token in journal
    assert logging.getLogger("httpx").level == logging.WARNING or logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING or logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    # also ensure urllib3/asyncio/ibapi still WARNING
    assert logging.getLogger("urllib3").getEffectiveLevel() == logging.WARNING


def test_telegram_token_not_in_logs(caplog):
    # ensure _sanitize redacts token even if health detail somehow contains it
    from app.services.watchdog.health import _sanitize
    assert "[REDACTED" in _sanitize("TELEGRAM_BOT_TOKEN=12345:ABC")
    assert "[REDACTED" in _sanitize("DATABASE_URL=postgresql://user:pass@host/db")

    # also ensure format_telegram_message sanitizes
    from app.services.watchdog.models import HealthResult, HealthStatus, ServiceName, ServiceSnapshot, ServiceState, NotificationEvent
    from app.services.watchdog.notifier import format_telegram_message
    hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, detail="token TELEGRAM_BOT_TOKEN=secret", underlying_error="TELEGRAM_BOT_TOKEN=secret")
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED, last_health=hr)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="h", health=hr)
    assert "secret" not in text
    assert "[REDACTED" in text
