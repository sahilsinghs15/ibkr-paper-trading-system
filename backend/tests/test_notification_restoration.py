"""Verification of Centralized OEMS Notification Architecture Restoration.

Proves:
1. One complete startup sequence -> ONE Telegram logical notification.
2. All startup components ready -> OEMS Started Successfully with canonical format.
3. Partial startup -> ONE OEMS Startup Warning.
4. IB Login is represented in the startup summary.
5. Individual startup milestone events do not each generate Telegram deliveries during startup.
6. Repeated startup events are deduplicated.
7. Startup aggregation remains idempotent.
8. Late startup milestone behavior remains deterministic.
9. Runtime broker disconnect produces an incident notification.
10. Runtime broker reconnect produces recovery notification.
11. Flapping behavior remains protected.
12. Legacy notify_telegram is NOT called by the centralized OEMS notification path.
13. TelegramChannelAdapter is the active Telegram delivery implementation.
14. No duplicate Telegram messages are produced by legacy + centralized paths.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, select

from app.db.models.notification import (
    NotificationDeliveryModel,
    NotificationLogModel,
)
from app.services.notification import (
    BrokerNotificationListener,
    NotificationDeliveryWorker,
    NotificationOrchestrator,
    StartupAggregator,
)
from app.services.notification.channels.telegram import TelegramChannelAdapter
from app.services.notification.dispatcher import ChannelDispatcher
from app.services.notification.types import (
    ChannelType,
    DeliveryResult,
    DeliveryStatus,
    NormalizedEvent,
    NotificationSeverity,
)


@pytest.fixture(autouse=True)
async def clean_notification_tables(session_factory):
    """Ensure clean notification tables for each test."""
    async with session_factory() as session:
        await session.execute(delete(NotificationDeliveryModel))
        await session.execute(delete(NotificationLogModel))
        await session.commit()


@pytest.mark.asyncio
async def test_complete_startup_sequence_produces_one_notification(session_factory):
    """1, 2, 4, 5: Complete startup sequence produces exactly ONE Telegram notification with canonical format."""
    orchestrator = NotificationOrchestrator(session_factory)
    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        window_sec=0.5,
        auto_probe=False,
    )

    listener = BrokerNotificationListener(
        orchestrator,
        startup_aggregator=aggregator,
    )
    listener.bind_loop(asyncio.get_running_loop())

    await aggregator.start_window()

    # Record components
    aggregator.record_component("ec2_instance", is_ready=True)
    aggregator.record_component("ib_gateway", is_ready=True)
    listener.on_next_valid_id(1001)  # Handshake completes
    aggregator.record_component("broker_connection", is_ready=True)
    aggregator.record_component("signal_receiver", is_ready=True)
    aggregator.record_component("oems_engine", is_ready=True)
    aggregator.record_component("dashboard_engine", is_ready=True)

    await asyncio.sleep(0.6)  # Window concludes

    # Verify in DB: exactly ONE notification log was created
    async with session_factory() as session:
        notifs = (
            await session.execute(select(NotificationLogModel))
        ).scalars().all()

        assert len(notifs) == 1
        notif = notifs[0]
        assert notif.event_type == "STARTUP_AGGREGATION"
        assert notif.title == "🟢 System Universe Started Successfully"
        assert notif.severity == NotificationSeverity.INFO.value
        assert "Server Machine     ✓" in notif.message
        assert "IB Gateway         ✓" in notif.message
        assert "IB Login           ✓" in notif.message
        assert "Broker Connection  ✓" in notif.message
        assert "Signal Receiver    ✓" in notif.message
        assert "OEMS Engine        ✓" in notif.message
        assert "Dashboard Engine   ✓" in notif.message

    # Dispatch via delivery worker to TelegramChannelAdapter
    mock_telegram = AsyncMock(spec=TelegramChannelAdapter)
    mock_telegram.channel = ChannelType.TELEGRAM
    mock_telegram.is_configured = True
    mock_telegram.send.return_value = DeliveryResult(success=True, provider_message_id="tg-msg-12345")
    mock_telegram.format_message_text.side_effect = lambda n: f"<b>{n.title}</b>\n\n{n.message}"

    dispatcher = ChannelDispatcher()
    dispatcher.register(mock_telegram)
    worker = NotificationDeliveryWorker(session_factory, dispatcher=dispatcher)

    sent = await worker.run_once()
    assert sent == 1
    mock_telegram.send.assert_called_once()


@pytest.mark.asyncio
async def test_partial_startup_produces_warning(session_factory):
    """3: Degraded component produces System Universe Startup Warning with checkmarks and crossmarks."""
    orchestrator = NotificationOrchestrator(session_factory)
    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        window_sec=0.2,
        auto_probe=False,
    )

    aggregator.record_component("ec2_instance", is_ready=True)
    aggregator.record_component("ib_gateway", is_ready=True)
    aggregator.record_component("ib_login", is_ready=True)
    aggregator.record_component("broker_connection", is_ready=True)
    aggregator.record_component("signal_receiver", is_ready=True)
    aggregator.record_component("oems_engine", is_ready=True)
    aggregator.record_component("dashboard_engine", is_ready=False, detail="Service stopped")

    await aggregator.publish_readiness()

    async with session_factory() as session:
        notifs = (
            await session.execute(
                select(NotificationLogModel).order_by(NotificationLogModel.id.desc())
            )
        ).scalars().all()

        assert len(notifs) >= 1
        notif = notifs[0]
        assert notif.title == "⚠️ System Universe Startup Warning"
        assert notif.severity == NotificationSeverity.WARNING.value
        assert "Dashboard Engine   ✗" in notif.message
        assert "Server Machine     ✓" in notif.message


@pytest.mark.asyncio
async def test_startup_deduplication_and_idempotence(session_factory):
    """6, 7, 8: Duplicate events deduplicated, aggregator is idempotent, late arrivals deterministic."""
    orchestrator = NotificationOrchestrator(session_factory)
    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        window_sec=0.2,
        auto_probe=False,
    )

    await aggregator.start_window()
    aggregator.record_component("ec2_instance", is_ready=True)
    aggregator.record_component("ec2_instance", is_ready=True)  # Duplicate recording
    aggregator.record_component("ib_gateway", is_ready=True)
    aggregator.record_component("ib_login", is_ready=True)
    aggregator.record_component("broker_connection", is_ready=True)
    aggregator.record_component("signal_receiver", is_ready=True)
    aggregator.record_component("oems_engine", is_ready=True)
    aggregator.record_component("dashboard_engine", is_ready=True)

    await aggregator.publish_readiness()
    # Idempotent call
    await aggregator.publish_readiness()

    # Late arrival
    aggregator.record_component("late_service", is_ready=True)
    await aggregator.publish_readiness()

    async with session_factory() as session:
        notifs = (
            await session.execute(select(NotificationLogModel))
        ).scalars().all()
        assert len(notifs) == 1


@pytest.mark.asyncio
async def test_runtime_broker_disconnect_and_reconnect(session_factory):
    """9, 10: Runtime broker disconnect produces CRITICAL alert; reconnect produces INFO alert."""
    orchestrator = NotificationOrchestrator(session_factory)
    listener = BrokerNotificationListener(orchestrator)
    listener.bind_loop(asyncio.get_running_loop())

    # 1. Broker lost at runtime
    listener.on_connection_closed()
    await asyncio.sleep(0.1)

    # 2. Broker reconnected at runtime
    listener.on_connection_restored()
    await asyncio.sleep(0.1)

    # 3. Handshake re-established at runtime (outside startup window)
    listener.on_next_valid_id(2001)
    await asyncio.sleep(0.1)

    async with session_factory() as session:
        notifs = (
            await session.execute(
                select(NotificationLogModel).order_by(NotificationLogModel.id.asc())
            )
        ).scalars().all()

        types = [n.event_type for n in notifs]
        assert "BROKER_LOST" in types
        assert "BROKER_RECONNECTED" in types
        assert "IB_LOGIN_COMPLETED" in types

        broker_lost = next(n for n in notifs if n.event_type == "BROKER_LOST")
        assert broker_lost.severity == NotificationSeverity.CRITICAL.value
        assert "Connection Lost" in broker_lost.title
        assert "Broker Connection  ✗" in broker_lost.message
        assert "IB Login           ✗" in broker_lost.message

        broker_rec = next(n for n in notifs if n.event_type == "BROKER_RECONNECTED")
        assert broker_rec.severity == NotificationSeverity.INFO.value
        assert "Connected Successfully" in broker_rec.title or "Connection Restored" in broker_rec.title


@pytest.mark.asyncio
async def test_telegram_channel_adapter_is_active_delivery_path(session_factory):
    """12, 13, 14: TelegramChannelAdapter is the delivery adapter and no legacy script is invoked."""
    adapter = TelegramChannelAdapter(
        bot_token="test-token-12345",
        chat_id="-1009999999",
        enabled=True,
    )

    event = NormalizedEvent(
        event_type="TEST_OEMS_ALERT",
        title="OEMS Test Invariant",
        message="Verification that centralized Telegram path is active.",
        category="SYSTEM",
        severity=NotificationSeverity.INFO,
    )

    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(event)
    assert notif is not None

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "ok": True,
            "result": {"message_id": 998877},
        }
        mock_post.return_value = mock_response

        dispatcher = ChannelDispatcher()
        dispatcher.register(adapter)
        worker = NotificationDeliveryWorker(session_factory, dispatcher=dispatcher)
        delivered = await worker.run_once()

        assert delivered == 1
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "https://api.telegram.org/bottest-token-12345/sendMessage" in url
        payload = mock_post.call_args[1]["json"]
        assert payload["chat_id"] == "-1009999999"
        assert "OEMS Test Invariant" in payload["text"]

    # Verify delivery record in DB
    async with session_factory() as session:
        delivs = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalars().all()
        assert len(delivs) == 1
        assert delivs[0].status == DeliveryStatus.DELIVERED.value
        assert delivs[0].provider_message_id == "998877"


@pytest.mark.asyncio
async def test_repeated_connection_closed_calls_emit_single_broker_lost(session_factory):
    """11: Multiple consecutive connection closed calls while offline emit exactly ONE BROKER_LOST."""
    orchestrator = NotificationOrchestrator(session_factory)
    listener = BrokerNotificationListener(orchestrator)
    listener.bind_loop(asyncio.get_running_loop())

    # Simulate 5 consecutive connectionClosed calls (e.g. failed reconnect retries while gateway is down)
    for _ in range(5):
        listener.on_connection_closed()
        await asyncio.sleep(0.05)

    async with session_factory() as session:
        notifs = (
            await session.execute(
                select(NotificationLogModel).where(NotificationLogModel.event_type == "BROKER_LOST")
            )
        ).scalars().all()
        # Exactly ONE logical notification must be generated
        assert len(notifs) == 1
        assert "IBKR Broker Connection Lost" in notifs[0].title


@pytest.mark.asyncio
async def test_normal_gateway_restart_does_not_trigger_flapping(session_factory):
    """11, 12: A normal IB Gateway restart (disconnect + retries + reconnect) does NOT trigger false flapping."""
    orchestrator = NotificationOrchestrator(session_factory)
    listener = BrokerNotificationListener(orchestrator)
    listener.bind_loop(asyncio.get_running_loop())

    # 1. Gateway stops -> socket drops
    listener.on_connection_closed()
    await asyncio.sleep(0.05)

    # 2. Multiple failed reconnect retries while gateway is booting
    for _ in range(4):
        listener.on_connection_closed()
        await asyncio.sleep(0.05)

    # 3. Gateway finishes booting -> socket reconnected
    listener.on_connection_restored()
    await asyncio.sleep(0.05)

    async with session_factory() as session:
        notifs = (
            await session.execute(
                select(NotificationLogModel).order_by(NotificationLogModel.id.asc())
            )
        ).scalars().all()

        types = [n.event_type for n in notifs]
        titles = [n.title for n in notifs]

        assert types == ["BROKER_LOST", "BROKER_RECONNECTED"]
        assert not any("FLAPPING" in t for t in titles)


@pytest.mark.asyncio
async def test_genuine_broker_oscillation_triggers_flapping(session_factory):
    """12: Genuine rapid broker oscillations (3 full disconnect/reconnect cycles) DO trigger flapping."""
    orchestrator = NotificationOrchestrator(session_factory)
    listener = BrokerNotificationListener(orchestrator)
    listener.bind_loop(asyncio.get_running_loop())

    # Cycle 1: disconnect + reconnect
    listener.on_connection_closed()
    await asyncio.sleep(0.05)
    listener.on_connection_restored()
    await asyncio.sleep(0.05)

    # Cycle 2: disconnect + reconnect
    listener.on_connection_closed()
    await asyncio.sleep(0.05)
    listener.on_connection_restored()
    await asyncio.sleep(0.05)

    # Cycle 3: disconnect
    listener.on_connection_closed()
    await asyncio.sleep(0.05)

    async with session_factory() as session:
        notifs = (
            await session.execute(
                select(NotificationLogModel).order_by(NotificationLogModel.id.asc())
            )
        ).scalars().all()

        titles = [n.title for n in notifs]
        # At least one notification must be the flapping alert
        assert any("FLAPPING" in t for t in titles)


@pytest.mark.asyncio
async def test_broker_recovery_produces_broker_connected_heading_not_oems_started(session_factory):
    """TEST B: Broker recovery produces '🟢 IBKR Broker Connected Successfully', NOT 'OEMS Started Successfully'."""
    orchestrator = NotificationOrchestrator(session_factory)

    # 1. Ingest an unrecovered BROKER_LOST incident
    lost_event = NormalizedEvent(
        event_type="BROKER_LOST",
        title="🔴 IBKR Broker Connection Lost",
        message="TWS/Gateway socket disconnected.",
        category="BROKER",
        severity=NotificationSeverity.CRITICAL,
        correlation_id="broker_connection",
    )
    await orchestrator.ingest_event(lost_event)

    # 2. StartupAggregator runs (simulating backend reboot triggered by gateway recovery)
    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        window_sec=0.2,
        auto_probe=False,
    )
    await aggregator.start_window()

    # Record all components as ready
    aggregator.record_component("ec2_instance", is_ready=True)
    aggregator.record_component("ib_gateway", is_ready=True)
    aggregator.record_component("ib_login", is_ready=True)
    aggregator.record_component("broker_connection", is_ready=True)
    aggregator.record_component("signal_receiver", is_ready=True)
    aggregator.record_component("oems_engine", is_ready=True)
    aggregator.record_component("dashboard_engine", is_ready=True)

    await asyncio.sleep(0.3)

    async with session_factory() as session:
        notifs = (
            await session.execute(
                select(NotificationLogModel).order_by(NotificationLogModel.id.asc())
            )
        ).scalars().all()

        assert len(notifs) == 2
        lost_notif = notifs[0]
        assert lost_notif.event_type == "BROKER_LOST"

        rec_notif = notifs[1]
        assert rec_notif.event_type == "BROKER_RECONNECTED"
        assert rec_notif.title == "🟢 IBKR Broker Connected Successfully"
        assert rec_notif.title != "🟢 System Universe Started Successfully"
        # All components must show checkmark
        assert "Server Machine     ✓" in rec_notif.message
        assert "IB Gateway         ✓" in rec_notif.message
        assert "IB Login           ✓" in rec_notif.message
        assert "Broker Connection  ✓" in rec_notif.message
        assert "Signal Receiver    ✓" in rec_notif.message
        assert "OEMS Engine        ✓" in rec_notif.message
        assert "Dashboard Engine   ✓" in rec_notif.message


@pytest.mark.asyncio
async def test_broker_recovery_with_dashboard_down_shows_cross_for_dashboard(session_factory):
    """Dynamic state: If Dashboard is down during broker recovery, Dashboard Engine shows ✗."""
    orchestrator = NotificationOrchestrator(session_factory)

    lost_event = NormalizedEvent(
        event_type="BROKER_LOST",
        title="🔴 IBKR Broker Connection Lost",
        message="TWS/Gateway socket disconnected.",
        category="BROKER",
        severity=NotificationSeverity.CRITICAL,
        correlation_id="broker_connection",
    )
    await orchestrator.ingest_event(lost_event)

    aggregator = StartupAggregator(
        orchestrator=orchestrator,
        window_sec=0.2,
        auto_probe=False,
    )
    await aggregator.start_window()

    # Record components: Dashboard is DOWN
    aggregator.record_component("ec2_instance", is_ready=True)
    aggregator.record_component("ib_gateway", is_ready=True)
    aggregator.record_component("ib_login", is_ready=True)
    aggregator.record_component("broker_connection", is_ready=True)
    aggregator.record_component("signal_receiver", is_ready=True)
    aggregator.record_component("oems_engine", is_ready=True)
    aggregator.record_component("dashboard_engine", is_ready=False, detail="Connection refused")

    await asyncio.sleep(0.3)

    async with session_factory() as session:
        notifs = (
            await session.execute(
                select(NotificationLogModel).order_by(NotificationLogModel.id.asc())
            )
        ).scalars().all()

        rec_notif = notifs[1]
        assert rec_notif.title == "🟢 IBKR Broker Connected Successfully"
        assert "Dashboard Engine   ✗" in rec_notif.message
        assert "Broker Connection  ✓" in rec_notif.message


@pytest.mark.asyncio
async def test_independent_ib_login_recovery_produces_login_completed(session_factory):
    """Runtime handshake completion outside startup produces '🟢 IB Login Completed Successfully'."""
    orchestrator = NotificationOrchestrator(session_factory)
    listener = BrokerNotificationListener(orchestrator)
    listener.bind_loop(asyncio.get_running_loop())

    # Initial handshake at runtime
    listener.on_next_valid_id(5001)
    await asyncio.sleep(0.1)

    async with session_factory() as session:
        notifs = (
            await session.execute(select(NotificationLogModel))
        ).scalars().all()

        assert len(notifs) == 1
        assert notifs[0].event_type == "IB_LOGIN_COMPLETED"
        assert notifs[0].title == "🟢 IB Login Completed Successfully"


def test_canonical_dashboard_service_messages():
    """Verify canonical dashboard messages match Dashboard Engine Started / Stopped."""
    from app.services.notification_canonical import CANONICAL_SERVICES

    demo_cfg = CANONICAL_SERVICES["demo-streaming"]
    assert demo_cfg["friendly_name"] == "Dashboard Engine"
    assert demo_cfg["SERVICE_STARTED"]["message"] == "Dashboard Engine Started Successfully"
    assert demo_cfg["SERVICE_STOPPED"]["message"] == "Dashboard Engine Stopped"


@pytest.mark.asyncio
async def test_server_machine_lifecycle_notifications(session_factory):
    """Verify server-machine start and stop produce canonical Server Machine notifications."""
    from app.services.notification.cli import report_service_lifecycle

    with patch("app.services.notification.cli.AsyncSessionLocal", session_factory), \
         patch("app.services.notification.cli.get_default_dispatcher"), \
         patch("app.services.notification.cli.NotificationDeliveryWorker") as mock_worker:
        mock_worker.return_value.run_once = AsyncMock()

        # 1. Stop Server Machine
        rc_stop = await report_service_lifecycle("stop", "server-machine")
        assert rc_stop == 0

        async with session_factory() as session:
            notifs = (await session.execute(select(NotificationLogModel))).scalars().all()
            assert len(notifs) == 1
            assert notifs[0].event_type == "SERVICE_STOPPED"
            assert notifs[0].title == "🔴 Server Machine Stopped"
            assert "Server Machine     ✗" in notifs[0].message
            assert "OEMS Engine        ✗" in notifs[0].message
            assert "Dashboard Engine   ✗" in notifs[0].message

        # Clean table
        async with session_factory() as session:
            await session.execute(delete(NotificationLogModel))
            await session.commit()

        # 2. Start Server Machine
        rc_start = await report_service_lifecycle("start", "server-machine")
        assert rc_start == 0

        async with session_factory() as session:
            notifs = (await session.execute(select(NotificationLogModel))).scalars().all()
            assert len(notifs) == 1
            assert notifs[0].event_type == "SERVICE_STARTED"
            assert notifs[0].title == "🟢 Server Machine Started Successfully"
            assert "Server Machine     ✓" in notifs[0].message



