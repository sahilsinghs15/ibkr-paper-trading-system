"""Comprehensive tests for Phase 2 Notification Intelligence and OEMS Event Integrations."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.db.models.account import AccountModel
from app.db.models.account_loss_state import AccountLossStateModel
from app.db.models.broker_position import BrokerPositionModel, PositionReconcileRunModel
from app.db.models.kill_switch import (
    KillSwitchOperationModel,
)
from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.kill_switch import (
    KillSwitchService,
    clear_account_kill_switch,
)
from app.services.loss_threshold_monitor import LossThresholdMonitor
from app.services.notification.broker_listener import BrokerNotificationListener
from app.services.notification.intelligence import (
    NotificationIntelligenceEngine,
)
from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.startup import StartupAggregator
from app.services.notification.types import (
    NormalizedEvent,
    NotificationSeverity,
    NotificationStatus,
)
from app.services.position_reconciler import PositionReconciler, ReconcileDiff


@pytest.fixture(autouse=True)
async def _cleanup_notifications(session_factory):
    """Clean up notification and test tables before and after each test."""
    async with session_factory() as session, session.begin():
        await session.execute(sa.delete(NotificationDeliveryModel))
        await session.execute(sa.delete(NotificationLogModel))
        await session.execute(sa.delete(KillSwitchOperationModel))
        await session.execute(sa.delete(AccountLossStateModel))
        await session.execute(sa.delete(PositionReconcileRunModel))
        await session.execute(sa.delete(BrokerPositionModel))
    yield
    async with session_factory() as session, session.begin():
        await session.execute(sa.delete(NotificationDeliveryModel))
        await session.execute(sa.delete(NotificationLogModel))
        await session.execute(sa.delete(KillSwitchOperationModel))
        await session.execute(sa.delete(AccountLossStateModel))
        await session.execute(sa.delete(PositionReconcileRunModel))
        await session.execute(sa.delete(BrokerPositionModel))


# =====================================================================
# 1. Intelligence Engine: Cooldown & Hourly Volume Limits
# =====================================================================


@pytest.mark.asyncio
async def test_cooldown_suppression(session_factory):
    """Event within cooldown period is suppressed; event after cooldown is admitted."""
    engine = NotificationIntelligenceEngine()
    orchestrator = NotificationOrchestrator(
        session_factory, intelligence_engine=engine
    )

    ev1 = NormalizedEvent(
        event_type="TEST_COOLDOWN_EVENT",
        title="Test Event 1",
        message="First event",
        category="SYSTEM",
        severity=NotificationSeverity.INFO,
        correlation_id="test_scope_1",
    )

    # First event should be admitted
    notif1 = await orchestrator.ingest_event(ev1)
    assert notif1 is not None
    assert notif1.status == NotificationStatus.PENDING.value

    # Immediate second identical event should be suppressed by cooldown
    ev2 = NormalizedEvent(
        event_type="TEST_COOLDOWN_EVENT",
        title="Test Event 2",
        message="Second event",
        category="SYSTEM",
        severity=NotificationSeverity.INFO,
        correlation_id="test_scope_1",
    )
    notif2 = await orchestrator.ingest_event(ev2)
    assert notif2 is not None
    assert notif2.status == NotificationStatus.SUPPRESSED.value
    assert notif2.suppressed_reason == "COOLDOWN"

    # Verify that no delivery was created for suppressed event
    async with session_factory() as session:
        delivs = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif2.id
                )
            )
        ).scalars().all()
        assert len(delivs) == 0


@pytest.mark.asyncio
async def test_hourly_limit_suppression(session_factory):
    """Events beyond hourly volume limit are suppressed with HOURLY_LIMIT reason."""
    engine = NotificationIntelligenceEngine()
    # Force small limit for test
    engine.get_hourly_limit = lambda sev: 2  # type: ignore[assignment]
    engine.get_cooldown_seconds = lambda sev, ev: 0.0  # type: ignore[assignment]

    orchestrator = NotificationOrchestrator(
        session_factory, intelligence_engine=engine
    )

    for i in range(2):
        ev = NormalizedEvent(
            event_type=f"HOURLY_TEST_{i}",
            title=f"Hourly Test {i}",
            message=f"Message {i}",
            severity=NotificationSeverity.INFO,
            details={"symbol": f"SYM_{i}"},
        )
        res = await orchestrator.ingest_event(ev)
        assert res is not None
        assert res.status == NotificationStatus.PENDING.value

    # 3rd event should exceed hourly limit
    ev_excess = NormalizedEvent(
        event_type="HOURLY_TEST_EXCESS",
        title="Excess Event",
        message="Should be throttled",
        severity=NotificationSeverity.INFO,
        details={"symbol": "SYM_EXCESS"},
    )
    res_excess = await orchestrator.ingest_event(ev_excess)
    assert res_excess is not None
    assert res_excess.status == NotificationStatus.SUPPRESSED.value
    assert res_excess.suppressed_reason == "HOURLY_LIMIT"


# =====================================================================
# 2. Intelligence Engine: Flapping Detection
# =====================================================================


@pytest.mark.asyncio
async def test_flapping_suppression_and_alert(session_factory):
    """Rapid state oscillations trigger a single FLAPPING alert then suppress until stabilized."""
    engine = NotificationIntelligenceEngine()
    engine.get_cooldown_seconds = lambda sev, ev: 0.0  # type: ignore[assignment]

    orchestrator = NotificationOrchestrator(
        session_factory, intelligence_engine=engine
    )

    # Event 1 & 2: normal
    for i in range(2):
        ev = NormalizedEvent(
            event_type="STATE_CHANGE",
            title="Normal Change",
            message=f"Change {i}",
            category="FLAP_TEST",
            correlation_id="flapper_1",
        )
        res = await orchestrator.ingest_event(ev)
        assert res is not None
        assert res.status == NotificationStatus.PENDING.value

    # Event 3: reaches threshold -> triggers flapping alert
    ev3 = NormalizedEvent(
        event_type="STATE_CHANGE",
        title="Normal Change",
        message="Change 2",
        category="FLAP_TEST",
        correlation_id="flapper_1",
    )
    res3 = await orchestrator.ingest_event(ev3)
    assert res3 is not None
    assert res3.status == NotificationStatus.PENDING.value
    assert "FLAPPING" in res3.title

    # Event 4: suppressed due to active flapping
    ev4 = NormalizedEvent(
        event_type="STATE_CHANGE",
        title="Normal Change",
        message="Change 3",
        category="FLAP_TEST",
        correlation_id="flapper_1",
    )
    res4 = await orchestrator.ingest_event(ev4)
    assert res4 is not None
    assert res4.status == NotificationStatus.SUPPRESSED.value
    assert res4.suppressed_reason == "FLAPPING"


# =====================================================================
# 3. Intelligence Engine: Incident / Recovery Correlation
# =====================================================================


@pytest.mark.asyncio
async def test_recovery_suppressed_when_no_prior_incident(session_factory):
    """A recovery notification is suppressed if no prior incident occurred."""
    orchestrator = NotificationOrchestrator(session_factory)

    rec_ev = NormalizedEvent(
        event_type="SERVICE_RECOVERED",
        title="Service Recovered",
        message="Service is healthy now",
        category="SERVICE",
        severity=NotificationSeverity.INFO,
        correlation_id="svc_component_a",
    )
    res = await orchestrator.ingest_event(rec_ev)
    assert res is not None
    assert res.status == NotificationStatus.SUPPRESSED.value
    assert res.suppressed_reason == "UNALERTED_INCIDENT"


@pytest.mark.asyncio
async def test_recovery_admitted_when_prior_incident_alerted(session_factory):
    """A recovery notification is admitted when matching prior incident was dispatched."""
    orchestrator = NotificationOrchestrator(session_factory)

    # 1. Incident event
    inc_ev = NormalizedEvent(
        event_type="SERVICE_OUTAGE",
        title="Service Outage",
        message="Service has failed",
        category="SERVICE",
        severity=NotificationSeverity.CRITICAL,
        correlation_id="svc_component_b",
    )
    inc_record = await orchestrator.ingest_event(inc_ev)
    assert inc_record is not None
    assert inc_record.status == NotificationStatus.PENDING.value

    # 2. Recovery event
    rec_ev = NormalizedEvent(
        event_type="SERVICE_RECOVERED",
        title="Service Restored",
        message="Service is back up",
        category="SERVICE",
        severity=NotificationSeverity.INFO,
        correlation_id="svc_component_b",
    )
    rec_record = await orchestrator.ingest_event(rec_ev)
    assert rec_record is not None
    assert rec_record.status == NotificationStatus.PENDING.value
    assert rec_record.suppressed_reason is None


# =====================================================================
# 4. Broker Lifecycle Integration Tests
# =====================================================================


@pytest.mark.asyncio
async def test_broker_notification_listener(session_factory):
    """BrokerNotificationListener emits BROKER_LOST and BROKER_RECONNECTED events."""
    orchestrator = NotificationOrchestrator(session_factory)
    client_mock = MagicMock()
    client_mock._connect_host = "127.0.0.1"
    client_mock._connect_port = 4001
    client_mock._connect_client_id = 99

    listener = BrokerNotificationListener(orchestrator, client_mock)
    listener.bind_loop(asyncio.get_running_loop())

    # Simulate connection closed callback
    t1 = listener.on_connection_closed()
    if t1 is not None:
        await t1

    async with session_factory() as session:
        lost_notifs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "BROKER_LOST"
                )
            )
        ).scalars().all()
        assert len(lost_notifs) == 1
        assert lost_notifs[0].severity == NotificationSeverity.CRITICAL.value
        assert "disconnected" in lost_notifs[0].message
        assert lost_notifs[0].correlation_id == "broker_connection"

    # Simulate connection restored callback
    t2 = listener.on_connection_restored()
    if t2 is not None:
        await t2

    async with session_factory() as session:
        rec_notifs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "BROKER_RECONNECTED"
                )
            )
        ).scalars().all()
        assert len(rec_notifs) == 1
        assert rec_notifs[0].severity == NotificationSeverity.INFO.value
        assert "reconnected" in rec_notifs[0].message


# =====================================================================
# 5. Kill Switch Integration Tests
# =====================================================================


@pytest.mark.asyncio
async def test_kill_switch_activated_and_completed_notifications(session_factory):
    """KillSwitchService emits KILL_SWITCH_ACTIVATED and KILL_SWITCH_COMPLETED events."""
    orchestrator = NotificationOrchestrator(session_factory)

    # Seed an account
    async with session_factory() as session, session.begin():
        acc = await session.get(AccountModel, 101)
        if acc is None:
            acc = AccountModel(
                id=101,
                name="Account 101",
                ibkr_account="DU10101",
                total_margin=Decimal(50000),
                enabled=True,
                loss_threshold=Decimal(-500),
            )
            session.add(acc)

    ks_service = KillSwitchService(
        session_factory=session_factory,
        notification_orchestrator=orchestrator,
    )

    op, created = await ks_service.initiate_square_off(
        account_id=101, requested_by="operator"
    )
    assert created is True
    await asyncio.sleep(0.15)

    # Verify KILL_SWITCH_ACTIVATED notification
    async with session_factory() as session:
        act_notifs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "KILL_SWITCH_ACTIVATED"
                )
            )
        ).scalars().all()
        assert len(act_notifs) == 1
        assert act_notifs[0].severity == NotificationSeverity.CRITICAL.value
        assert "DU10101" in act_notifs[0].title
        assert act_notifs[0].correlation_id == "kill_switch_101"

    # Simulate reconcile and finalize
    await ks_service._reconcile_and_finalize(op.operation_id, 101, [])
    await asyncio.sleep(0.15)

    # Verify KILL_SWITCH_COMPLETED notification
    async with session_factory() as session:
        fin_notifs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "KILL_SWITCH_COMPLETED"
                )
            )
        ).scalars().all()
        assert len(fin_notifs) == 1
        assert fin_notifs[0].severity == NotificationSeverity.INFO.value
        assert fin_notifs[0].correlation_id == "kill_switch_101"

    # Clear kill switch
    cleared = await clear_account_kill_switch(
        session_factory,
        101,
        cleared_by="operator",
        notification_orchestrator=orchestrator,
    )
    assert cleared == 1
    await asyncio.sleep(0.15)

    # Verify KILL_SWITCH_CLEARED notification
    async with session_factory() as session:
        clr_notifs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "KILL_SWITCH_CLEARED"
                )
            )
        ).scalars().all()
        assert len(clr_notifs) == 1
        assert clr_notifs[0].severity == NotificationSeverity.INFO.value


# =====================================================================
# 6. Loss Threshold Monitor Integration Tests
# =====================================================================


@pytest.mark.asyncio
async def test_loss_threshold_breached_and_recovery_notifications(session_factory):
    """LossThresholdMonitor emits breach and recovery events via orchestrator."""
    orchestrator = NotificationOrchestrator(session_factory)

    # Seed an account with negative threshold
    async with session_factory() as session, session.begin():
        acc = await session.get(AccountModel, 202)
        if acc is None:
            acc = AccountModel(
                id=202,
                name="Account 202",
                ibkr_account="DU20202",
                total_margin=Decimal(50000),
                enabled=True,
                loss_threshold=Decimal(-100),
            )
            session.add(acc)
        else:
            acc.loss_threshold = Decimal(-100)
            acc.enabled = True

    monitor = LossThresholdMonitor(
        session_factory=session_factory,
        notification_orchestrator=orchestrator,
    )

    # Mock realized P&L to breach threshold (-150 <= -100)
    with patch(
        "app.services.loss_threshold_monitor._sum_realised",
        AsyncMock(return_value=Decimal(-150)),
    ):
        res = await monitor.evaluate_account(202)
        assert res is not None
        await asyncio.sleep(0.15)

        async with session_factory() as session:
            breach_notifs = (
                await session.execute(
                    select(NotificationLogModel).where(
                        NotificationLogModel.event_type == "LOSS_THRESHOLD_BREACHED"
                    )
                )
            ).scalars().all()
            assert len(breach_notifs) == 1
            assert breach_notifs[0].severity == NotificationSeverity.WARNING.value
            assert breach_notifs[0].correlation_id == "loss_threshold_202"

    # Subsequent evaluation while still breached should NOT emit duplicate
    with patch(
        "app.services.loss_threshold_monitor._sum_realised",
        AsyncMock(return_value=Decimal(-160)),
    ):
        res2 = await monitor.evaluate_account(202)
        assert res2 is None
        await asyncio.sleep(0.15)

        async with session_factory() as session:
            count = (
                await session.execute(
                    select(sa.func.count()).select_from(NotificationLogModel)
                )
            ).scalar_one()
            assert count == 1  # No duplicate breach emitted

    # Recovery evaluation (P&L rises to -50 > -100)
    with patch(
        "app.services.loss_threshold_monitor._sum_realised",
        AsyncMock(return_value=Decimal(-50)),
    ):
        await monitor.evaluate_account(202)
        await asyncio.sleep(0.15)

        async with session_factory() as session:
            rec_notifs = (
                await session.execute(
                    select(NotificationLogModel).where(
                        NotificationLogModel.event_type == "LOSS_THRESHOLD_RECOVERED"
                    )
                )
            ).scalars().all()
            assert len(rec_notifs) == 1
            assert rec_notifs[0].severity == NotificationSeverity.INFO.value
            assert rec_notifs[0].correlation_id == "loss_threshold_202"


# =====================================================================
# 7. Position Reconciler / Rogue Position Attribution
# =====================================================================


@pytest.mark.asyncio
async def test_position_reconciler_rogue_trade_detection_and_resolution(
    session_factory,
):
    """PositionReconciler emits ROGUE_TRADE_DETECTED and RESOLVED without fabricated execution fields."""
    orchestrator = NotificationOrchestrator(session_factory)
    client_mock = MagicMock()

    reconciler = PositionReconciler(
        session_factory=session_factory,
        client=client_mock,
        notification_orchestrator=orchestrator,
        rogue_confirm_sweeps=1,
    )

    # Seed an account
    async with session_factory() as session, session.begin():
        acc = await session.get(AccountModel, 303)
        if acc is None:
            acc = AccountModel(
                id=303,
                name="Account 303",
                ibkr_account="DU30303",
                total_margin=Decimal(50000),
                enabled=True,
            )
            session.add(acc)

    # Mock reconcile diff detection
    diff = ReconcileDiff(
        kind="BROKER_ORPHAN",
        ibkr_account="DU30303",
        account_id=303,
        symbol="AAPL",
        sec_type="STK",
        con_id=12345,
        broker_qty=100.0,
        ledger_qty=None,
        in_flight=False,
    )

    # Execute reconcile pass with diff
    client_mock.is_connected = MagicMock(return_value=True)
    client_mock.request_positions_async = AsyncMock(return_value=([], False))

    with patch("app.services.position_reconciler.classify_reconcile_diffs", return_value=[diff]):
        await reconciler.run_once()
        await asyncio.sleep(0.15)

    # Verify ROGUE_TRADE_DETECTED notification
    async with session_factory() as session:
        rogue_notifs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "ROGUE_TRADE_DETECTED"
                )
            )
        ).scalars().all()
        assert len(rogue_notifs) == 1
        det = rogue_notifs[0]
        assert det.severity == NotificationSeverity.CRITICAL.value
        # Operator-facing title uses human-readable label ("Broker Ghost"), not raw code
        assert "AAPL" in det.title
        assert "Broker Ghost" in det.title or "Unexpected position change" in det.title
        # Invariant check: MUST NOT fabricate order_id or exec_id
        assert "exec_id" not in det.payload
        assert "order_id" not in det.payload
        assert det.payload["broker_qty"] == 100
        assert det.payload["symbol"] == "AAPL"

    # Next reconcile pass with no diff -> RESOLVED
    with patch("app.services.position_reconciler.classify_reconcile_diffs", return_value=[]):
        await reconciler.run_once()
        await asyncio.sleep(0.15)

    # Verify ROGUE_TRADE_RESOLVED notification
    async with session_factory() as session:
        res_notifs = (
            await session.execute(
                select(NotificationLogModel).where(
                    NotificationLogModel.event_type == "ROGUE_TRADE_RESOLVED"
                )
            )
        ).scalars().all()
        assert len(res_notifs) == 1
        res = res_notifs[0]
        assert res.severity == NotificationSeverity.INFO.value
        assert "AAPL" in res.title
        assert res.correlation_id == det.correlation_id


# =====================================================================
# 8. Startup Aggregation Tests
# =====================================================================


@pytest.mark.asyncio
async def test_startup_aggregator_all_ready(session_factory):
    """StartupAggregator produces GLOBAL OEMS READY when all critical components succeed."""
    orchestrator = NotificationOrchestrator(session_factory)
    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        critical_components=("database", "broker", "worker_pool"),
        window_sec=1.0,
    )

    aggregator.record_component("database", is_ready=True, detail="PostgreSQL ready")
    aggregator.record_component("broker", is_ready=True, detail="Gateway 4001 ready")
    aggregator.record_component("worker_pool", is_ready=True, detail="Workers running")

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
        assert notifs[0].title == "GLOBAL OEMS READY"
        assert notifs[0].severity == NotificationSeverity.INFO.value
        assert "All critical systems nominal" in notifs[0].message


@pytest.mark.asyncio
async def test_startup_aggregator_partial_warning(session_factory):
    """StartupAggregator produces PARTIAL STARTUP WARNING when a critical component fails."""
    orchestrator = NotificationOrchestrator(session_factory)
    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        critical_components=("database", "broker", "worker_pool"),
        window_sec=1.0,
    )

    aggregator.record_component("database", is_ready=True, detail="PostgreSQL ready")
    aggregator.record_component("broker", is_ready=False, detail="Gateway timeout")
    aggregator.record_component("worker_pool", is_ready=True, detail="Workers running")

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
        assert notifs[0].title == "PARTIAL STARTUP WARNING"
        assert notifs[0].severity == NotificationSeverity.WARNING.value
        assert "degraded at startup" in notifs[0].message
        assert notifs[0].payload["ready"] is False
        assert "broker" in notifs[0].payload["failed"]


# =====================================================================
# 9. Failure Isolation Test
# =====================================================================


@pytest.mark.asyncio
async def test_notification_ingest_failure_isolation(session_factory):
    """Notification failure with fail_silent=True returns None without throwing."""
    # Broken session factory that fails on connect
    broken_session_factory = MagicMock(side_effect=RuntimeError("DB Connection dropped"))
    orchestrator = NotificationOrchestrator(broken_session_factory)

    ev = NormalizedEvent(
        event_type="TEST_CRITICAL_EVENT",
        title="Critical Alert",
        message="Failure test",
        severity=NotificationSeverity.CRITICAL,
    )

    # Should not raise exception
    res = await orchestrator.ingest_event(ev, fail_silent=True)
    assert res is None
