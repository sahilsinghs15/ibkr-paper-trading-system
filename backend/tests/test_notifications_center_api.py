"""Tests for canonical notification contract, filtering, read state, and Notification Center APIs."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.db.models.event import EventLogModel
from app.db.models.user import UserModel
from app.db.repositories.event_repository import EventRepository
from app.db.session import AsyncSessionLocal
from app.services.notification_canonical import (
    CANONICAL_SERVICES,
    format_canonical_notification,
)
from demo_streaming.api import create_demo_app


@pytest.fixture
def canonical_fixtures():
    return CANONICAL_SERVICES


def test_canonical_dictionary_completeness(canonical_fixtures):
    """Verify all 4 required services exist and have both started and stopped definitions."""
    expected_services = {"ibgateway", "trading-backend", "webhook-ingest", "demo-streaming"}
    assert set(canonical_fixtures.keys()) == expected_services

    assert canonical_fixtures["ibgateway"]["friendly_name"] == "IB Gateway"
    assert canonical_fixtures["ibgateway"]["SERVICE_STARTED"]["message"] == "IB Gateway Started Successfully"
    assert canonical_fixtures["ibgateway"]["SERVICE_STOPPED"]["message"] == "IB Gateway Stopped"

    assert canonical_fixtures["trading-backend"]["friendly_name"] == "OEMS Engine"
    assert canonical_fixtures["trading-backend"]["SERVICE_STARTED"]["message"] == "OEMS Engine Started Successfully"
    assert canonical_fixtures["trading-backend"]["SERVICE_STOPPED"]["message"] == "OEMS Engine Stopped"

    assert canonical_fixtures["webhook-ingest"]["friendly_name"] == "Signal Receiver"
    assert canonical_fixtures["webhook-ingest"]["SERVICE_STARTED"]["message"] == "Signal Receiver Started Successfully"
    assert canonical_fixtures["webhook-ingest"]["SERVICE_STOPPED"]["message"] == "Signal Receiver Stopped"

    assert canonical_fixtures["demo-streaming"]["friendly_name"] == "Dashboard Engine"
    assert canonical_fixtures["demo-streaming"]["SERVICE_STARTED"]["message"] == "Dashboard Engine Started Successfully"
    assert canonical_fixtures["demo-streaming"]["SERVICE_STOPPED"]["message"] == "Dashboard Engine Stopped"


def test_format_canonical_notification_market_closed():
    """Verify MARKET_CLOSED formatting."""
    formatted = format_canonical_notification("MARKET_CLOSED", {"reason": "Thanksgiving"})
    assert formatted["icon"] == "📅"
    assert formatted["title"] == "Market closed — Thanksgiving"
    assert formatted["message"] == "Market closed — Thanksgiving"
    assert formatted["friendly_name"] == "Market status"


@pytest.mark.asyncio
async def test_notifications_api_flow():
    """Test /demo/notifications, /read, /mark-all-read with authentication and filtering."""
    redis_mock = MagicMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.xread = AsyncMock(return_value=[])

    demo_app = create_demo_app(
        session_factory=AsyncSessionLocal,
        redis=redis_mock,
        stream_name="positions:stream",
    )

    prefix = f"notif_test_{int(datetime.now(UTC).timestamp())}"
    async with AsyncSessionLocal() as session:
        repo = EventRepository(session)
        user = UserModel(
            email=f"{prefix}@example.com",
            password_hash="mock",
            role="admin",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        # 1. Approved: ibgateway start
        ev1 = await repo.append(
            process="systemd",
            kind="SERVICE_STARTED",
            detail={"service": "ibgateway", "action": "start"},
            idempotency_key=f"{prefix}:ibgateway:start",
        )
        # 2. Approved: demo-streaming stop
        ev2 = await repo.append(
            process="systemd",
            kind="SERVICE_STOPPED",
            detail={"service": "demo-streaming", "action": "stop"},
            idempotency_key=f"{prefix}:demo-streaming:stop",
        )
        # 3. Approved: market closed
        ev3 = await repo.append(
            process="session_clock",
            kind="MARKET_CLOSED",
            detail={"reason": "New Year's Day"},
            idempotency_key=f"{prefix}:market_closed",
        )
        # 4. EXCLUDED: unauthorized service
        ev4 = await repo.append(
            process="systemd",
            kind="SERVICE_STARTED",
            detail={"service": "unauthorized-service", "action": "start"},
            idempotency_key=f"{prefix}:unauth:start",
        )
        # 5. EXCLUDED: arbitrary kind
        ev5 = await repo.append(
            process="rms",
            kind="RMS_REJECTED",
            detail={"reason": "Risk limit"},
            idempotency_key=f"{prefix}:rms:reject",
        )
        await session.commit()

    assert ev1 is not None and ev2 is not None and ev3 is not None and ev4 is not None and ev5 is not None
    ev1_id: int = ev1.id
    ev2_id: int = ev2.id
    ev3_id: int = ev3.id
    ev4_id: int = ev4.id
    ev5_id: int = ev5.id

    token = create_access_token({"sub": str(user.id)})
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=demo_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Step 1: GET /demo/notifications
        res = await client.get("/demo/notifications", headers=headers)
        assert res.status_code == 200
        data = res.json()
        items = data["items"]
        item_ids = [it["id"] for it in items]

        # Verify filtering: approved ev1, ev2, ev3 present; ev4 and ev5 excluded
        assert ev1_id in item_ids
        assert ev2_id in item_ids
        assert ev3_id in item_ids
        assert ev4_id not in item_ids
        assert ev5_id not in item_ids

        # Verify canonical mapping and fields
        item_ev1 = next(it for it in items if it["id"] == ev1_id)
        assert item_ev1["title"] == "IB Gateway Started Successfully"
        assert item_ev1["icon"] == "🟢"
        assert item_ev1["is_read"] is False

        item_ev2 = next(it for it in items if it["id"] == ev2_id)
        assert item_ev2["title"] == "Dashboard Engine Stopped"
        assert item_ev2["icon"] == "🔴"
        assert item_ev2["is_read"] is False

        item_ev3 = next(it for it in items if it["id"] == ev3_id)
        assert item_ev3["title"] == "Market closed — New Year's Day"
        assert item_ev3["icon"] == "📅"
        assert item_ev3["is_read"] is False

        # Step 2: Mark single notification read (ev2)
        res_read = await client.post(f"/demo/notifications/{ev2_id}/read", headers=headers)
        assert res_read.status_code == 200
        assert res_read.json()["ok"] is True

        # Verify read status
        res_after = await client.get("/demo/notifications", headers=headers)
        data_after = res_after.json()
        item_ev2_after = next(it for it in data_after["items"] if it["id"] == ev2_id)
        assert item_ev2_after["is_read"] is True

        # Step 3: Mark all as read
        res_all = await client.post("/demo/notifications/mark-all-read", headers=headers)
        assert res_all.status_code == 200
        assert res_all.json()["unread_count"] == 0

        res_final = await client.get("/demo/notifications", headers=headers)
        data_final = res_final.json()
        assert data_final["unread_count"] == 0
        for it in data_final["items"]:
            if it["id"] in (ev1_id, ev2_id, ev3_id):
                assert it["is_read"] is True

        # Step 4: GET /demo/system-events returns canonical titles
        res_sys = await client.get(f"/demo/system-events?since_id={ev1_id - 1}", headers=headers)
        assert res_sys.status_code == 200
        sys_data = res_sys.json()
        sys_ids = [e["id"] for e in sys_data]
        assert ev1_id in sys_ids
        assert ev4_id not in sys_ids  # unauthorized service filtered out
        assert ev5_id not in sys_ids  # non-lifecycle kind filtered out

    # Cleanup test data
    async with AsyncSessionLocal() as session:
        for eid in (ev1_id, ev2_id, ev3_id, ev4_id, ev5_id):
            row = await session.get(EventLogModel, eid)
            if row:
                await session.delete(row)
        u_row = await session.get(UserModel, user.id)
        if u_row:
            await session.delete(u_row)
        await session.commit()


@pytest.mark.asyncio
async def test_orchestrator_frontend_telegram_sync():
    """Verify that events admitted by NotificationOrchestrator sync to /demo/notifications with identical title and state table."""
    from app.services.notification.orchestrator import NotificationOrchestrator
    from app.services.notification.types import NormalizedEvent, NotificationSeverity
    from app.db.models.notification import NotificationLogModel, NotificationDeliveryModel
    from sqlalchemy import select

    table_content = (
        "<pre>\n"
        "Server Machine     ✓\n"
        "IB Gateway         ✗\n"
        "IB Login           ✗\n"
        "Broker Connection  ✗\n"
        "Signal Receiver    ✓\n"
        "OEMS Engine        ✓\n"
        "Dashboard Engine   ✓\n"
        "</pre>"
    )
    title = "🔴 IB Gateway Stopped"
    dedupe = f"sync_test:{datetime.now(UTC).timestamp()}"

    event = NormalizedEvent(
        event_type="SERVICE_STOPPED",
        title=title,
        message=table_content,
        category="SYSTEM",
        severity=NotificationSeverity.CRITICAL,
        correlation_id="ibgateway",
        dedupe_key=dedupe,
        details={
            "service": "ibgateway",
            "action": "stop",
        },
    )

    orchestrator = NotificationOrchestrator(AsyncSessionLocal)
    notif = await orchestrator.ingest_event(event)
    assert notif is not None
    assert notif.title == title
    assert notif.message == table_content

    # Now verify that the frontend app GET /demo/notifications receives the exact same notification
    redis_mock = AsyncMock()
    app = create_demo_app(
        session_factory=AsyncSessionLocal,
        redis=redis_mock,
        stream_name="positions:stream",
    )

    prefix = f"sync_u_{int(datetime.now(UTC).timestamp())}"
    async with AsyncSessionLocal() as session:
        user = UserModel(
            email=f"{prefix}@test.com",
            password_hash="mock",
            role="admin",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    token = create_access_token({"sub": str(user.id)})
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        res = await client.get("/demo/notifications", headers=headers)
        assert res.status_code == 200
        items = res.json()["items"]
        matching = [it for it in items if it["title"] == title]
        assert len(matching) >= 1
        item = matching[0]
        # Must match Telegram message exactly
        assert item["message"] == table_content
        assert item["icon"] == "🔴"
        assert item["service"] == "ibgateway"

        # Verify GET /demo/system-events returns the exact same title, message, and icon
        res_sys = await client.get(f"/demo/system-events?since_id={item['id'] - 1}", headers=headers)
        assert res_sys.status_code == 200
        sys_items = res_sys.json()
        matching_sys = [it for it in sys_items if it["title"] == title]
        assert len(matching_sys) >= 1
        assert matching_sys[0]["message"] == table_content
        assert matching_sys[0]["icon"] == "🔴"

    # Cleanup
    async with AsyncSessionLocal() as session:
        u_row = await session.get(UserModel, user.id)
        if u_row:
            await session.delete(u_row)
        stmt_del = select(NotificationDeliveryModel).where(NotificationDeliveryModel.notification_id == notif.id)
        for d in (await session.execute(stmt_del)).scalars().all():
            await session.delete(d)
        n_row = await session.get(NotificationLogModel, notif.id)
        if n_row:
            await session.delete(n_row)
        stmt_ev = select(EventLogModel).where(EventLogModel.detail["notification_id"].astext == notif.notification_id)
        for ev in (await session.execute(stmt_ev)).scalars().all():
            await session.delete(ev)
        await session.commit()



@pytest.mark.asyncio
async def test_unread_count_agrees_between_feed_and_mark_read():
    """The badge must not change meaning depending on which endpoint answered.

    `/demo/notifications` counted every approved kind, while
    `/demo/notifications/{id}/read` recomputed the count with a different
    predicate that required `detail.service` to be in ALLOWED_SERVICES. That
    silently excluded ROGUE_TRADE_*, STARTUP_AGGREGATION, BROKER_* and
    LOSS_THRESHOLD_BREACHED -- none of which carry a `service` key -- so marking
    one item read could move the badge by more than one, or not at all.
    """
    redis_mock = MagicMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.xread = AsyncMock(return_value=[])

    demo_app = create_demo_app(
        session_factory=AsyncSessionLocal,
        redis=redis_mock,
        stream_name="positions:stream",
    )

    prefix = f"unread_sync_{int(datetime.now(UTC).timestamp() * 1000)}"
    async with AsyncSessionLocal() as session:
        repo = EventRepository(session)
        user = UserModel(
            email=f"{prefix}@example.com",
            password_hash="mock",
            role="admin",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        # A service event (has detail.service) and a rogue event (does not).
        # The old mark-read predicate counted only the first.
        svc = await repo.append(
            process="systemd",
            kind="SERVICE_STARTED",
            detail={"service": "ibgateway", "action": "start"},
            idempotency_key=f"{prefix}:svc",
        )
        rogue = await repo.append(
            process="reconcile",
            kind="ROGUE_TRADE_DETECTED",
            detail={"symbol": "TSLA", "rogue_type": "BROKER_ORPHAN"},
            idempotency_key=f"{prefix}:rogue",
        )
        await session.commit()

    assert svc is not None and rogue is not None
    token = create_access_token({"sub": str(user.id)})
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=demo_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        feed = (await client.get("/demo/notifications", headers=headers)).json()
        before = feed["unread_count"]
        # Both events are unread and both must be counted.
        assert before >= 2

        marked = (
            await client.post(
                f"/demo/notifications/{rogue.id}/read", headers=headers
            )
        ).json()

        refetched = (await client.get("/demo/notifications", headers=headers)).json()

        # The two endpoints must report the same number, and marking exactly one
        # item read must decrement by exactly one.
        assert marked["unread_count"] == refetched["unread_count"]
        assert marked["unread_count"] == before - 1
