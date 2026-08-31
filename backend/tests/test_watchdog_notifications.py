"""Watchdog notification deduplication and Telegram resilience."""

import asyncio

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.models import NotificationEvent, ServiceName, ServiceSnapshot, ServiceState
from app.services.watchdog.notifier import NotificationDeduplicator, NotificationQueue, format_telegram_message
from app.services.watchdog.telegram import TelegramClient


def test_format_contains_fields():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED, failure_reason="exit 1")
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="main-ec2", port=8001, attempt="2/5", reason="exit 1")
    assert "Trading Backend" in text or "BACKEND" in text or "backend" in text
    assert "8001" in text
    assert "2/5" in text
    # spec structure
    assert "WATCHDOG" in text
    assert "WHAT HAPPENED" in text
    assert "ERROR / FAILURE DETAIL" in text
    assert "IMPACT" in text
    assert "RECOVERY" in text


def test_dedup_suppresses_duplicate():
    d = NotificationDeduplicator(cooldown_seconds=60)
    assert d.should_send(ServiceName.BACKEND, NotificationEvent.FAILURE) is True
    d.mark_sent(ServiceName.BACKEND, NotificationEvent.FAILURE)
    assert d.should_send(ServiceName.BACKEND, NotificationEvent.FAILURE) is False
    # different event not suppressed
    assert d.should_send(ServiceName.BACKEND, NotificationEvent.RECOVERED) is True


def test_queue_dedup():
    settings = WatchdogSettings(telegram_enabled=False)
    tg = TelegramClient(None, None, enabled=False)
    q = NotificationQueue(tg, settings)
    q.dedup.cooldown = 60
    text = "hello"
    assert q.enqueue(ServiceName.BACKEND, NotificationEvent.FAILURE, text) is True
    assert q.enqueue(ServiceName.BACKEND, NotificationEvent.FAILURE, text) is False
    assert q.enqueue(ServiceName.BACKEND, NotificationEvent.FAILURE, text, force=True) is True


def test_telegram_not_configured_returns_false():
    client = TelegramClient(None, None, enabled=False)

    async def _run():
        ok = await client.send_message("hi")
        assert ok is False

    asyncio.run(_run())


def test_telegram_failure_does_not_raise():
    # enabled but invalid token — should retry and return False, not raise
    client = TelegramClient("bad_token", "123", enabled=True, timeout=0.5, max_retries=1)

    async def _run():
        ok = await client.send_message("hi")
        assert ok is False

    asyncio.run(_run())


def test_queue_worker_does_not_crash_on_telegram_failure():
    settings = WatchdogSettings(telegram_enabled=False)
    tg = TelegramClient(None, None, enabled=False)
    q = NotificationQueue(tg, settings)

    async def _run():
        await q.start()
        q.enqueue(ServiceName.BACKEND, NotificationEvent.FAILURE, "test", force=True)
        await asyncio.sleep(0.3)
        await q.stop()

    asyncio.run(_run())
