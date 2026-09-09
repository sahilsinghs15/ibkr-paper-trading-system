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

    assert canonical_fixtures["ibgateway"]["friendly_name"] == "Broker connection"
    assert canonical_fixtures["ibgateway"]["SERVICE_STARTED"]["message"] == "Broker connection started"
    assert canonical_fixtures["ibgateway"]["SERVICE_STOPPED"]["message"] == "Broker connection stopped"

    assert canonical_fixtures["trading-backend"]["friendly_name"] == "Trading system"
    assert canonical_fixtures["trading-backend"]["SERVICE_STARTED"]["message"] == "Trading system started"
    assert canonical_fixtures["trading-backend"]["SERVICE_STOPPED"]["message"] == "Trading system stopped"

    assert canonical_fixtures["webhook-ingest"]["friendly_name"] == "Market signal intake"
    assert canonical_fixtures["webhook-ingest"]["SERVICE_STARTED"]["message"] == "Market signal intake started"
    assert canonical_fixtures["webhook-ingest"]["SERVICE_STOPPED"]["message"] == "Market signal intake stopped"

    assert canonical_fixtures["demo-streaming"]["friendly_name"] == "Market data display"
    assert canonical_fixtures["demo-streaming"]["SERVICE_STARTED"]["message"] == "Market data display started"
    assert canonical_fixtures["demo-streaming"]["SERVICE_STOPPED"]["message"] == "Market data display stopped"


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
        assert item_ev1["title"] == "Broker connection started"
        assert item_ev1["icon"] == "🟢"
        assert item_ev1["is_read"] is False

        item_ev2 = next(it for it in items if it["id"] == ev2_id)
        assert item_ev2["title"] == "Market data display stopped"
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
