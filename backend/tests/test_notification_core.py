"""Comprehensive unit and integration tests for Phase 1 Notification Core and Telegram channel."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.channels.base import BaseChannelAdapter
from app.services.notification.channels.telegram import (
    TelegramChannelAdapter,
    _get_severity_icon,
)
from app.services.notification.dispatcher import ChannelDispatcher
from app.services.notification.normalizer import EventNormalizer
from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.types import (
    ChannelType,
    DeliveryResult,
    DeliveryStatus,
    NormalizedEvent,
    NotificationSeverity,
    NotificationStatus,
)
from app.services.notification.worker import NotificationDeliveryWorker


@pytest.fixture(autouse=True)
async def _cleanup_notifications(session_factory):
    """Ensure clean notification tables before and after each test."""
    async with session_factory() as session, session.begin():
        await session.execute(sa.delete(NotificationDeliveryModel))
        await session.execute(sa.delete(NotificationLogModel))
    yield
    async with session_factory() as session, session.begin():
        await session.execute(sa.delete(NotificationDeliveryModel))
        await session.execute(sa.delete(NotificationLogModel))


# =====================================================================
# 1. Event Normalizer Tests
# =====================================================================

def test_normalizer_from_normalized_event():
    """EventNormalizer returns the event unchanged if already a NormalizedEvent."""
    original = NormalizedEvent(
        event_type="TEST_EVENT",
        title="Test Title",
        message="Test Message",
        severity=NotificationSeverity.CRITICAL,
    )
    normalized = EventNormalizer.normalize(original)
    assert normalized is original
    assert normalized.severity == NotificationSeverity.CRITICAL


def test_normalizer_from_dict_with_defaults():
    """EventNormalizer extracts fields from a heterogeneous dict with sensible defaults."""
    raw = {
        "kind": "BROKER_DISCONNECTED",
        "detail": {"message": "Socket connection lost", "code": 502},
        "severity": "WARNING",
    }
    normalized = EventNormalizer.normalize(raw)
    assert normalized.event_type == "BROKER_DISCONNECTED"
    assert normalized.severity == NotificationSeverity.WARNING
    assert "Socket connection lost" in normalized.message
    assert normalized.category == "SYSTEM"
    assert normalized.details["code"] == 502


def test_normalizer_invalid_severity_fallback():
    """EventNormalizer falls back safely if severity string is unknown."""
    raw = {
        "event_type": "CUSTOM_ALERT",
        "title": "Alert Title",
        "message": "Some message",
        "severity": "NON_EXISTENT_LEVEL",
    }
    normalized = EventNormalizer.normalize(raw, default_severity=NotificationSeverity.INFO)
    assert normalized.severity == NotificationSeverity.INFO


# =====================================================================
# 2. Telegram Adapter Unit Tests
# =====================================================================

def test_telegram_severity_icons():
    """Verify icon mapping by severity and recovery semantics."""
    assert _get_severity_icon("CRITICAL") == "🚨"
    assert _get_severity_icon("WARNING") == "⚠️"
    assert _get_severity_icon("INFO") == "ℹ️"
    assert _get_severity_icon("CRITICAL", "ROGUE_TRADE_RESOLVED") == "🟢"
    assert _get_severity_icon("INFO", "BROKER_RECOVERED") == "🟢"


def test_telegram_format_message_escaping():
    """Telegram adapter escapes HTML characters to prevent parsing failures."""
    adapter = TelegramChannelAdapter(bot_token="fake_token", chat_id="12345", enabled=True)
    logical = NotificationLogModel(
        title="Alert with <tag> & symbols",
        message="Error details: x < 5 & y > 10",
        severity="WARNING",
        category="RISK",
    )
    text = adapter.format_message_text(logical)
    assert "&lt;tag&gt;" in text
    assert "&amp;" in text
    assert "<b>Alert with &lt;tag&gt; &amp; symbols</b>" in text
    assert "x &lt; 5 &amp; y &gt; 10" in text


@pytest.mark.asyncio
async def test_telegram_send_unconfigured():
    """Telegram adapter returns a non-retryable error if credentials are missing."""
    adapter = TelegramChannelAdapter(bot_token=None, chat_id=None, enabled=True)
    delivery = NotificationDeliveryModel(
        delivery_id="DELIV-1",
        channel="TELEGRAM",
        recipient="",
    )
    logical = NotificationLogModel(
        notification_id="NOTIF-1",
        title="Test",
        message="Msg",
        severity="INFO",
        category="SYSTEM",
    )
    result = await adapter.send(delivery, logical)
    assert not result.success
    assert not result.is_retryable
    assert "not configured" in (result.error_message or "")


@pytest.mark.asyncio
async def test_telegram_send_success():
    """Telegram adapter parses successful 200 OK and captures message_id."""
    adapter = TelegramChannelAdapter(bot_token="test_token", chat_id="123456", enabled=True)
    delivery = NotificationDeliveryModel(
        delivery_id="DELIV-1",
        channel="TELEGRAM",
        recipient="123456",
    )
    logical = NotificationLogModel(
        notification_id="NOTIF-1",
        title="Test System",
        message="Running fine",
        severity="INFO",
        category="SYSTEM",
    )

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"ok": True, "result": {"message_id": 98765}}
    fake_response.text = '{"ok": true, "result": {"message_id": 98765}}'

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_response):
        result = await adapter.send(delivery, logical)

    assert result.success
    assert result.provider_message_id == "98765"
    assert not result.is_retryable


@pytest.mark.asyncio
async def test_telegram_send_transient_error():
    """Telegram adapter classifies 429 and 5xx as retryable."""
    adapter = TelegramChannelAdapter(bot_token="test_token", chat_id="123456", enabled=True)
    delivery = NotificationDeliveryModel(
        delivery_id="DELIV-1",
        channel="TELEGRAM",
        recipient="123456",
    )
    logical = NotificationLogModel(
        notification_id="NOTIF-1",
        title="Test System",
        message="Transient issue",
        severity="WARNING",
        category="SYSTEM",
    )

    fake_response = MagicMock()
    fake_response.status_code = 429
    fake_response.json.return_value = {
        "ok": False,
        "description": "Too Many Requests",
        "parameters": {"retry_after": 5},
    }
    fake_response.text = '{"ok": false, "description": "Too Many Requests"}'

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_response):
        result = await adapter.send(delivery, logical)

    assert not result.success
    assert result.is_retryable
    assert "429" in (result.error_message or "")


@pytest.mark.asyncio
async def test_telegram_send_permanent_error():
    """Telegram adapter classifies 400/403 as non-retryable."""
    adapter = TelegramChannelAdapter(bot_token="test_token", chat_id="123456", enabled=True)
    delivery = NotificationDeliveryModel(
        delivery_id="DELIV-1",
        channel="TELEGRAM",
        recipient="123456",
    )
    logical = NotificationLogModel(
        notification_id="NOTIF-1",
        title="Test System",
        message="Bad Chat ID",
        severity="WARNING",
        category="SYSTEM",
    )

    fake_response = MagicMock()
    fake_response.status_code = 400
    fake_response.json.return_value = {"ok": False, "description": "Chat not found"}
    fake_response.text = '{"ok": false, "description": "Chat not found"}'

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_response):
        result = await adapter.send(delivery, logical)

    assert not result.success
    assert not result.is_retryable
    assert "Chat not found" in (result.error_message or "")


# =====================================================================
# 3. Channel Dispatcher Tests
# =====================================================================

@pytest.mark.asyncio
async def test_dispatcher_routing():
    """ChannelDispatcher dispatches to registered adapter or returns unsupported channel error."""
    dispatcher = ChannelDispatcher()

    class MockAdapter(BaseChannelAdapter):
        @property
        def channel(self) -> ChannelType:
            return ChannelType.TELEGRAM

        async def send(self, delivery, logical) -> DeliveryResult:
            return DeliveryResult(success=True, provider_message_id="mock-123")

    dispatcher.register(MockAdapter())

    delivery_tg = NotificationDeliveryModel(
        delivery_id="D1", channel="TELEGRAM", recipient="chat1"
    )
    logical = NotificationLogModel(notification_id="N1", title="T", message="M", severity="INFO", category="C")

    res_tg = await dispatcher.dispatch(delivery_tg, logical)
    assert res_tg.success
    assert res_tg.provider_message_id == "mock-123"

    delivery_unknown = NotificationDeliveryModel(
        delivery_id="D2", channel="UNKNOWN_CHANNEL", recipient="target"
    )
    res_un = await dispatcher.dispatch(delivery_unknown, logical)
    assert not res_un.success
    assert not res_un.is_retryable
    assert "No adapter registered" in (res_un.error_message or "")


# =====================================================================
# 4. Persistence, Orchestrator & Deduplication Tests
# =====================================================================

@pytest.mark.asyncio
async def test_orchestrator_ingest_and_deduplication(session_factory):
    """Orchestrator creates notification and delivery outbox records, and deduplicates identical keys."""
    orchestrator = NotificationOrchestrator(
        session_factory, default_telegram_chat_id="test_chat_1"
    )

    event_payload = {
        "kind": "BROKER_OUTAGE",
        "title": "TWS Disconnected",
        "message": "Gateway connection lost on port 4001",
        "severity": "CRITICAL",
        "category": "BROKER",
        "dedupe_key": "broker_outage_port_4001_test1",
    }

    # First ingest: creates record
    notif1 = await orchestrator.ingest_event(event_payload)
    assert notif1 is not None
    assert notif1.severity == "CRITICAL"
    assert notif1.dedupe_key == "broker_outage_port_4001_test1"
    assert notif1.status == "PENDING"

    async with session_factory() as session:
        delivs = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif1.id
                )
            )
        ).scalars().all()
        assert len(delivs) == 1
        assert delivs[0].channel == "TELEGRAM"
        assert delivs[0].status == "PENDING"
        assert delivs[0].recipient == "test_chat_1"

    # Second ingest with identical dedupe_key: returns existing, does NOT create new deliveries
    notif2 = await orchestrator.ingest_event(event_payload)
    assert notif2 is not None
    assert notif2.id == notif1.id
    assert notif2.notification_id == notif1.notification_id

    async with session_factory() as session:
        delivs_after = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif1.id
                )
            )
        ).scalars().all()
        assert len(delivs_after) == 1


# =====================================================================
# 5. Delivery Worker & Retry Lifecycle Tests
# =====================================================================

@pytest.mark.asyncio
async def test_delivery_worker_success_flow(session_factory):
    """Worker processes pending delivery, marks DELIVERED, and updates logical status to COMPLETED."""
    dispatcher = ChannelDispatcher()

    class SuccessAdapter(BaseChannelAdapter):
        @property
        def channel(self) -> ChannelType:
            return ChannelType.TELEGRAM

        async def send(self, delivery, logical) -> DeliveryResult:
            return DeliveryResult(success=True, provider_message_id="tg_msg_777")

    dispatcher.register(SuccessAdapter())

    orchestrator = NotificationOrchestrator(session_factory, default_telegram_chat_id="chat_777")
    notif = await orchestrator.ingest_event({
        "event_type": "TEST_ALERT",
        "title": "Alert 1",
        "message": "Everything OK",
        "severity": "INFO",
    })
    assert notif is not None

    worker = NotificationDeliveryWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        poll_interval_sec=0.1,
    )

    processed = await worker.run_once(batch_size=10)
    assert processed == 1

    # Verify outcome in DB
    async with session_factory() as session:
        delivery = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalar_one()
        assert delivery.status == DeliveryStatus.DELIVERED.value
        assert delivery.provider_message_id == "tg_msg_777"
        assert delivery.delivered_at is not None
        assert delivery.attempt_count == 1

        logical = (
            await session.execute(
                select(NotificationLogModel).where(NotificationLogModel.id == notif.id)
            )
        ).scalar_one()
        assert logical.status == NotificationStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_delivery_worker_retryable_failure_and_backoff(session_factory):
    """Worker handles transient failure by scheduling retry with backoff."""
    dispatcher = ChannelDispatcher()

    class TransientFailAdapter(BaseChannelAdapter):
        @property
        def channel(self) -> ChannelType:
            return ChannelType.TELEGRAM

        async def send(self, delivery, logical) -> DeliveryResult:
            return DeliveryResult(
                success=False,
                error_message="HTTP 429 Rate limited",
                is_retryable=True,
            )

    dispatcher.register(TransientFailAdapter())

    orchestrator = NotificationOrchestrator(session_factory, default_telegram_chat_id="chat_transient")
    notif = await orchestrator.ingest_event({
        "event_type": "TRANSIENT_TEST",
        "title": "Transient Alert",
        "message": "Retry me",
        "severity": "WARNING",
    })
    assert notif is not None

    worker = NotificationDeliveryWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        poll_interval_sec=0.1,
        max_retries=3,
        initial_retry_backoff_sec=5.0,
    )

    # First attempt: transitions to RETRYING with next_retry_at in the future
    processed = await worker.run_once(batch_size=10)
    assert processed == 1

    async with session_factory() as session:
        delivery = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalar_one()
        assert delivery.status == DeliveryStatus.RETRYING.value
        assert delivery.attempt_count == 1
        assert delivery.next_retry_at is not None
        assert delivery.next_retry_at > datetime.now(UTC)

    # Second run immediately: next_retry_at is in the future, so not eligible!
    processed_immediate = await worker.run_once(batch_size=10)
    assert processed_immediate == 0


@pytest.mark.asyncio
async def test_delivery_worker_max_retries_exhaustion(session_factory):
    """Worker transitions to FAILED when max_retries is reached."""
    dispatcher = ChannelDispatcher()

    class TransientFailAdapter(BaseChannelAdapter):
        @property
        def channel(self) -> ChannelType:
            return ChannelType.TELEGRAM

        async def send(self, delivery, logical) -> DeliveryResult:
            return DeliveryResult(
                success=False,
                error_message="Permanent 500 Server Error",
                is_retryable=True,
            )

    dispatcher.register(TransientFailAdapter())

    orchestrator = NotificationOrchestrator(session_factory, default_telegram_chat_id="chat_exhaust")
    notif = await orchestrator.ingest_event({
        "event_type": "EXHAUST_TEST",
        "title": "Exhaust Alert",
        "message": "Exhaust me",
        "severity": "CRITICAL",
    })
    assert notif is not None

    worker = NotificationDeliveryWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        poll_interval_sec=0.1,
        max_retries=1,  # Only 1 attempt allowed
        initial_retry_backoff_sec=1.0,
    )

    processed = await worker.run_once(batch_size=10)
    assert processed == 1

    async with session_factory() as session:
        delivery = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalar_one()
        # Max retries exhausted on first attempt
        assert delivery.status == DeliveryStatus.FAILED.value
        assert delivery.attempt_count == 1
        assert delivery.next_retry_at is None

        logical = (
            await session.execute(
                select(NotificationLogModel).where(NotificationLogModel.id == notif.id)
            )
        ).scalar_one()
        assert logical.status == NotificationStatus.FAILED.value


# =====================================================================
# 6. Failure Isolation Tests
# =====================================================================

@pytest.mark.asyncio
async def test_delivery_worker_failure_isolation(session_factory):
    """An unhandled adapter crash or failure on one delivery does not break other deliveries."""
    dispatcher = ChannelDispatcher()

    call_count = 0

    class CrashyAdapter(BaseChannelAdapter):
        @property
        def channel(self) -> ChannelType:
            return ChannelType.TELEGRAM

        async def send(self, delivery, logical) -> DeliveryResult:
            nonlocal call_count
            call_count += 1
            if delivery.recipient == "crash_target":
                raise RuntimeError("Simulated network crash")
            return DeliveryResult(success=True, provider_message_id=f"msg_{delivery.recipient}")

    dispatcher.register(CrashyAdapter())

    orchestrator = NotificationOrchestrator(session_factory)

    notif_bad = await orchestrator.ingest_event(
        {"event_type": "BAD", "title": "Bad", "message": "Crash"},
        recipient_overrides={"TELEGRAM": "crash_target"},
    )
    notif_good = await orchestrator.ingest_event(
        {"event_type": "GOOD", "title": "Good", "message": "Succeed"},
        recipient_overrides={"TELEGRAM": "good_target"},
    )
    assert notif_bad is not None
    assert notif_good is not None

    worker = NotificationDeliveryWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        max_retries=3,
        initial_retry_backoff_sec=5.0,
    )

    processed = await worker.run_once(batch_size=10)
    assert processed == 2
    assert call_count == 2

    async with session_factory() as session:
        bad_deliv = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif_bad.id
                )
            )
        ).scalar_one()
        # Bad delivery was captured safely without crashing the worker
        assert bad_deliv.status == DeliveryStatus.RETRYING.value
        assert "Dispatcher crashed" in str(bad_deliv.error_details)

        good_deliv = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif_good.id
                )
            )
        ).scalar_one()
        # Good delivery succeeded normally
        assert good_deliv.status == DeliveryStatus.DELIVERED.value
        assert good_deliv.provider_message_id == "msg_good_target"
