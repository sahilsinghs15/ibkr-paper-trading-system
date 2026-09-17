"""Comprehensive tests for Phase 3 Production Hardening, Shadow Validation, and Legacy Cutover."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.db.models.broker_position import BrokerPositionModel, PositionReconcileRunModel
from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.dispatcher import ChannelDispatcher
from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.startup import StartupAggregator
from app.services.notification.types import (
    ChannelType,
    DeliveryResult,
    DeliveryStatus,
    NormalizedEvent,
    NotificationSeverity,
    NotificationStatus,
)
from app.services.notification.worker import NotificationDeliveryWorker
from app.services.notification_canonical import send_canonical_telegram
from app.services.position_reconciler import PositionReconciler


@pytest.fixture(autouse=True)
async def _cleanup_notifications(session_factory):
    """Clean up notification and test tables before and after each test."""
    async with session_factory() as session, session.begin():
        await session.execute(sa.delete(NotificationDeliveryModel))
        await session.execute(sa.delete(NotificationLogModel))
        await session.execute(sa.delete(PositionReconcileRunModel))
        await session.execute(sa.delete(BrokerPositionModel))
    yield
    async with session_factory() as session, session.begin():
        await session.execute(sa.delete(NotificationDeliveryModel))
        await session.execute(sa.delete(NotificationLogModel))
        await session.execute(sa.delete(PositionReconcileRunModel))
        await session.execute(sa.delete(BrokerPositionModel))


# =====================================================================
# 1. Multi-Process Concurrency & Advisory Locking
# =====================================================================


@pytest.mark.asyncio
async def test_concurrent_ingestion_advisory_locking(session_factory):
    """Multiple concurrent ingestion tasks for the same event and correlation key are serialized and cooldowned."""
    orchestrator = NotificationOrchestrator(session_factory)

    async def _ingest():
        event = NormalizedEvent(
            event_type="CONCURRENT_PROBE",
            title="Concurrent Ingest Probe",
            message="Testing advisory locking under concurrent ingestion",
            category="TEST",
            severity=NotificationSeverity.WARNING,
            source="test_runner",
            correlation_id="concurrent_probe_key",
        )
        return await orchestrator.ingest_event(event)

    # Launch 2 concurrent ingestions simultaneously (below flapping threshold 3)
    results = await asyncio.gather(*[_ingest() for _ in range(2)])
    assert all(r is not None for r in results)

    async with session_factory() as session:
        logs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "CONCURRENT_PROBE"
                )
            )
        ).scalars().all()

        admitted = [l for l in logs if l.status != NotificationStatus.SUPPRESSED.value]
        suppressed = [l for l in logs if l.status == NotificationStatus.SUPPRESSED.value]

        # Exactly 1 should be admitted, and 1 should be suppressed under cooldown
        assert len(admitted) == 1
        assert len(suppressed) == 1
        assert suppressed[0].suppressed_reason == "COOLDOWN"


# =====================================================================
# 2. Worker Crash Recovery for Stuck SENDING Deliveries
# =====================================================================


@pytest.mark.asyncio
async def test_worker_crash_recovery_stuck_sending(session_factory):
    """Worker recovers deliveries orphaned in SENDING status past lease timeout."""
    orchestrator = NotificationOrchestrator(session_factory)
    worker = NotificationDeliveryWorker(session_factory)

    event = NormalizedEvent(
        event_type="WORKER_LEASE_PROBE",
        title="Crash Recovery Probe",
        message="Simulating worker process crash while in flight",
        category="SYSTEM",
        severity=NotificationSeverity.CRITICAL,
        source="test",
        correlation_id="crash_probe_1",
    )
    notif = await orchestrator.ingest_event(event)
    assert notif is not None
    assert notif.status == NotificationStatus.PENDING.value

    # Simulate 2 deliveries stuck in SENDING from a previous crash:
    # - Deliv 1: attempt 1/3 (should transition to RETRYING with next_retry_at = now)
    # - Deliv 2: attempt 3/3 (should transition to FAILED with parent updated)
    past_time = datetime.now(UTC) - timedelta(seconds=120)

    async with session_factory() as session, session.begin():
        d1 = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalars().first()
        assert d1 is not None
        d1.status = DeliveryStatus.SENDING.value
        d1.last_attempt_at = past_time
        d1.attempt_count = 1

        # Create second delivery stuck at max retries
        d2 = NotificationDeliveryModel(
            delivery_id="DELIV-STUCK-MAX",
            notification_id=notif.id,
            channel="SMS_PROBE",
            provider="test_prov",
            recipient="default",
            status=DeliveryStatus.SENDING.value,
            last_attempt_at=past_time,
            attempt_count=3,
            max_retries=3,
        )
        session.add(d2)

    # Run stuck delivery recovery
    recovered_count = await worker.recover_stuck_deliveries(lease_timeout_sec=30.0)
    assert recovered_count == 2

    async with session_factory() as session:
        delivs = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalars().all()

        retrying_item = next(d for d in delivs if d.id == d1.id)
        failed_item = next(d for d in delivs if d.delivery_id == "DELIV-STUCK-MAX")

        assert retrying_item.status == DeliveryStatus.RETRYING.value
        assert retrying_item.next_retry_at is not None
        assert retrying_item.next_retry_at <= datetime.now(UTC)

        assert failed_item.status == DeliveryStatus.FAILED.value
        assert failed_item.error_details.get("reason") == "STUCK_LEASE_EXHAUSTED"


# =====================================================================
# 3. Telegram 429 retry_after Handling
# =====================================================================


@pytest.mark.asyncio
async def test_telegram_retry_after_backoff(session_factory):
    """Telegram HTTP 429 response containing retry_after is respected by the delivery worker."""
    dispatcher = ChannelDispatcher()
    mock_adapter = MagicMock()
    mock_adapter.channel = ChannelType.TELEGRAM
    mock_adapter.send = AsyncMock(
        return_value=DeliveryResult(
            success=False,
            error_message="HTTP 429 Too Many Requests",
            is_retryable=True,
            error_details={"http_status": 429, "retry_after": 45},
        )
    )
    dispatcher.register(mock_adapter)

    orchestrator = NotificationOrchestrator(session_factory)
    worker = NotificationDeliveryWorker(
        session_factory,
        dispatcher=dispatcher,
        initial_retry_backoff_sec=5.0,
    )

    event = NormalizedEvent(
        event_type="RATE_LIMIT_TEST",
        title="Telegram Rate Limit Test",
        message="Testing 429 retry_after schedule",
        category="TEST",
        severity=NotificationSeverity.INFO,
        source="test",
        correlation_id="rate_limit_probe",
    )
    notif = await orchestrator.ingest_event(event)
    assert notif is not None

    processed = await worker.run_once()
    assert processed == 1

    async with session_factory() as session:
        deliv = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalars().one()

        assert deliv.status == DeliveryStatus.RETRYING.value
        assert deliv.attempt_count == 1
        # next_retry_at should be approximately now + 45s (not 5s default backoff)
        expected_min = datetime.now(UTC) + timedelta(seconds=40)
        assert deliv.next_retry_at is not None
        assert deliv.next_retry_at >= expected_min


# =====================================================================
# 4. Startup Aggregator Hardening: Out-of-Order & Degraded Components
# =====================================================================


@pytest.mark.asyncio
async def test_startup_aggregator_out_of_order_and_degraded(session_factory):
    """StartupAggregator handles out-of-order component events and strictly alerts on degradation."""
    orchestrator = NotificationOrchestrator(session_factory)
    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        critical_components=("database", "broker", "worker_pool", "position_reconciler"),
    )

    # Arrive out-of-order: worker_pool first, then position_reconciler, then database
    aggregator.record_component("worker_pool", is_ready=True, detail="10 workers ready")
    aggregator.record_component("position_reconciler", is_ready=True, detail="Active")
    aggregator.record_component("database", is_ready=True, detail="Connected")
    # Broker arrives degraded!
    aggregator.record_component("broker", is_ready=False, detail="Gateway socket refused on 127.0.0.1:4001")

    await aggregator.publish_readiness()

    async with session_factory() as session:
        notifs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "STARTUP_AGGREGATION"
                )
            )
        ).scalars().all()

        assert len(notifs) == 1
        notif = notifs[0]
        assert notif.severity == NotificationSeverity.WARNING.value
        assert "PARTIAL STARTUP WARNING" in notif.title
        assert notif.payload["ready"] is False
        assert "broker" in notif.payload["failed"]


@pytest.mark.asyncio
async def test_startup_aggregator_late_arrival(session_factory):
    """Components arriving after the aggregation window has closed do not crash or corrupt state."""
    orchestrator = NotificationOrchestrator(session_factory)
    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        critical_components=("database", "broker"),
    )

    aggregator.record_component("database", is_ready=True)
    aggregator.record_component("broker", is_ready=True)
    await aggregator.publish_readiness()

    # Late arrival after publish
    aggregator.record_component("late_service", is_ready=True, detail="Started after window")

    # Publishing again should be an idempotent no-op
    await aggregator.publish_readiness()

    async with session_factory() as session:
        count = (
            await session.execute(
                select(sa.func.count()).select_from(NotificationLogModel)
            )
        ).scalar_one()
        assert count == 1


# =====================================================================
# 5. Incident / Recovery Durability Across Process Restarts
# =====================================================================


@pytest.mark.asyncio
async def test_incident_recovery_durability_across_restarts(session_factory):
    """Recovery correlation resolves against durable database state even if the process restarts."""
    # Process 1: receives BROKER_LOST incident and delivers it
    orchestrator_proc1 = NotificationOrchestrator(session_factory)
    dispatcher_proc1 = ChannelDispatcher()
    mock_adapter = MagicMock()
    mock_adapter.channel = ChannelType.TELEGRAM
    mock_adapter.send = AsyncMock(
        return_value=DeliveryResult(
            success=True,
            provider_message_id="TG-MSG-1001",
            is_retryable=False,
        )
    )
    dispatcher_proc1.register(mock_adapter)
    worker_proc1 = NotificationDeliveryWorker(session_factory, dispatcher=dispatcher_proc1)

    lost_event = NormalizedEvent(
        event_type="BROKER_LOST",
        title="IBKR Socket Disconnected",
        message="Socket closed",
        category="BROKER",
        severity=NotificationSeverity.CRITICAL,
        source="tws_client",
        correlation_id="broker_connection",
    )
    inc_notif = await orchestrator_proc1.ingest_event(lost_event)
    assert inc_notif is not None
    await worker_proc1.run_once()

    # Simulate Process 1 crashing and a new Process 2 starting up
    del orchestrator_proc1
    del worker_proc1

    orchestrator_proc2 = NotificationOrchestrator(session_factory)
    dispatcher_proc2 = ChannelDispatcher()
    mock_adapter2 = MagicMock()
    mock_adapter2.channel = ChannelType.TELEGRAM
    mock_adapter2.send = AsyncMock(
        return_value=DeliveryResult(
            success=True,
            provider_message_id="TG-MSG-1002",
            is_retryable=False,
        )
    )
    dispatcher_proc2.register(mock_adapter2)
    worker_proc2 = NotificationDeliveryWorker(session_factory, dispatcher=dispatcher_proc2)

    rec_event = NormalizedEvent(
        event_type="BROKER_RECONNECTED",
        title="IBKR Socket Reconnected",
        message="Socket restored",
        category="BROKER",
        severity=NotificationSeverity.INFO,
        source="tws_client",
        correlation_id="broker_connection",
    )
    rec_notif = await orchestrator_proc2.ingest_event(rec_event)
    assert rec_notif is not None
    # Admitted because it durably found the alerted incident from Process 1
    assert rec_notif.status != NotificationStatus.SUPPRESSED.value

    processed = await worker_proc2.run_once()
    assert processed == 1


# =====================================================================
# 6. Shadow Mode Validation
# =====================================================================


@pytest.mark.asyncio
async def test_shadow_mode_validation(session_factory):
    """In shadow mode, events are durably persisted without dispatching external Telegram calls."""
    orchestrator = NotificationOrchestrator(session_factory, shadow_mode=True)
    worker = NotificationDeliveryWorker(session_factory)

    event = NormalizedEvent(
        event_type="SHADOW_PROBE",
        title="Shadow Mode Probe Alert",
        message="This alert should be durably recorded but not dispatched externally",
        category="TEST",
        severity=NotificationSeverity.WARNING,
        source="test",
        correlation_id="shadow_probe_1",
    )
    notif = await orchestrator.ingest_event(event)
    assert notif is not None

    async with session_factory() as session:
        deliv = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalars().one()

        assert deliv.status == DeliveryStatus.DELIVERED.value
        assert deliv.provider_message_id == "SHADOW_DELIVERED"
        assert deliv.attempt_count == 0

    # Delivery worker should find 0 pending deliveries
    processed = await worker.run_once()
    assert processed == 0


# =====================================================================
# 7. Controlled Cutover & Elimination of Double Alerts
# =====================================================================


@pytest.mark.asyncio
async def test_controlled_cutover_elimination_of_double_alerts(session_factory):
    """PositionReconciler dispatches solely through orchestrator when present, eliminating legacy double calls."""
    from uuid import uuid4

    from app.broker.ibkr.positions import BrokerPositionLine
    from app.db.models.account import AccountModel

    unique_sym = f"AAPL_{uuid4().hex[:6].upper()}"

    async with session_factory() as session, session.begin():
        acc = (await session.execute(select(AccountModel))).scalars().first()
        if acc is None:
            acc = AccountModel(id=1, name="TestAcc", ibkr_account="U123")
            session.add(acc)
            await session.flush()
        target_ibkr_account = acc.ibkr_account

    broker_line = BrokerPositionLine(
        ibkr_account=target_ibkr_account,
        symbol=unique_sym,
        sec_type="STK",
        con_id=999123,
        currency="USD",
        exchange="SMART",
        quantity=10.0,
        avg_cost=150.0,
    )

    orchestrator = NotificationOrchestrator(session_factory)
    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    mock_client.request_positions_async = AsyncMock(return_value=([broker_line], False))

    reconciler = PositionReconciler(
        session_factory=session_factory,
        client=mock_client,
        notification_orchestrator=orchestrator,
    )

    with patch("app.services.position_reconciler.send_canonical_telegram", new_callable=AsyncMock) as legacy_mock:
        await reconciler.run_once()
        await asyncio.sleep(0.05)

        # Legacy direct Telegram call was NOT made because orchestrator was active!
        assert legacy_mock.await_count == 0

        # Centralized notification was durably created!
        async with session_factory() as session:
            notifs = (
                await session.execute(
                    select(NotificationLogModel).where(
                        NotificationLogModel.event_type == "ROGUE_TRADE_DETECTED",
                        NotificationLogModel.title.like(f"%{unique_sym}%"),
                    )
                )
            ).scalars().all()
            assert len(notifs) == 1


@pytest.mark.asyncio
async def test_legacy_send_canonical_telegram_decommissioned(monkeypatch):
    """send_canonical_telegram safely skips direct send when notification_legacy_telegram_enabled=False."""
    monkeypatch.setenv("NOTIFICATION_LEGACY_TELEGRAM_ENABLED", "false")
    res = await send_canonical_telegram("SERVICE_STARTED", {"service": "trading-backend"})
    # Returns True safely without sending external HTTP requests
    assert res is True
