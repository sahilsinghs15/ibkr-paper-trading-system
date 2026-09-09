"""Tests for GET /demo/system-events API endpoint."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.db.models.event import EventLogModel
from app.db.models.user import UserModel
from app.db.repositories.event_repository import EventRepository
from app.db.session import AsyncSessionLocal
from demo_streaming.api import create_demo_app


@pytest.mark.asyncio
class TestSystemEventsApi:
    """Validate system events retrieval API."""

    async def test_system_events_auth_and_filtering(self, monkeypatch):
        redis_mock = MagicMock()
        redis_mock.ping = AsyncMock(return_value=True)
        redis_mock.xread = AsyncMock(return_value=[])

        demo_app = create_demo_app(
            session_factory=AsyncSessionLocal,
            redis=redis_mock,
            stream_name="positions:stream",
        )

        async with AsyncSessionLocal() as session:
            repo = EventRepository(session)
            # Create a test user for auth testing
            test_email = f"events_test_{int(datetime.now(UTC).timestamp())}@example.com"
            user = UserModel(
                email=test_email,
                password_hash="mock",
                role="admin",
                is_active=True,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            # Insert sample events: two relevant system events, one unrelated kind
            ev1 = await repo.append(
                process="systemd",
                kind="SERVICE_STARTED",
                detail={"service": "ibgateway", "action": "start", "icon": "🟢", "message": "ibgateway started"},
                idempotency_key="test_api:ibgateway:start:1",
            )
            ev2 = await repo.append(
                process="session_clock",
                kind="MARKET_CLOSED",
                detail={"date": "2026-11-26", "reason": "Thanksgiving", "icon": "📅", "message": "Market closed — Thanksgiving"},
                idempotency_key="test_api:market_closed:2026-11-26",
            )
            ev3 = await repo.append(
                process="order_manager",
                kind="SIGNAL_PROCESSED",
                detail={"order_id": 999},
                idempotency_key="test_api:signal:999",
            )
            ev4 = await repo.append(
                process="systemd",
                kind="SERVICE_STOPPED",
                detail={"service": "ibgateway", "action": "stop", "icon": "🔴", "message": "ibgateway stopped"},
                idempotency_key="test_api:ibgateway:stop:1",
            )
            await session.commit()

        token = create_access_token({"sub": str(user.id)})
        auth_headers = {"Authorization": f"Bearer {token}"}

        async with AsyncClient(transport=ASGITransport(app=demo_app), base_url="http://test") as client:
            # 1. Unauthenticated request rejected when testing flag disabled
            monkeypatch.setenv("TRADINGAPP_TESTING", "0")
            unauth_resp = await client.get("/demo/system-events")
            assert unauth_resp.status_code == 401
            monkeypatch.setenv("TRADINGAPP_TESTING", "1")

            # 2. Authenticated request returns relevant kinds, filtering out SIGNAL_PROCESSED
            resp = await client.get("/demo/system-events", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            returned_ids = [e["id"] for e in data]
            assert ev1.id in returned_ids
            assert ev2.id in returned_ids
            assert ev4.id in returned_ids
            assert ev3.id not in returned_ids  # Non-system event filtered out!

            # 3. Test since_id filtering
            since_resp = await client.get(f"/demo/system-events?since_id={ev2.id}", headers=auth_headers)
            assert since_resp.status_code == 200
            since_data = since_resp.json()
            since_ids = [e["id"] for e in since_data]
            assert ev1.id not in since_ids
            assert ev2.id not in since_ids
            assert ev4.id in since_ids

            # 4. Test limit parameter
            limit_resp = await client.get("/demo/system-events?since_id=0&limit=1", headers=auth_headers)
            assert limit_resp.status_code == 200
            limit_data = limit_resp.json()
            assert len(limit_data) == 1

            # 5. Test negative since_id validation (422)
            invalid_resp = await client.get("/demo/system-events?since_id=-5", headers=auth_headers)
            assert invalid_resp.status_code == 422

            # 6. Verify payload contract fields
            sample = next(e for e in data if e["id"] == ev1.id)
            assert "id" in sample
            assert "ts" in sample
            assert "kind" in sample
            assert "detail" in sample
            assert sample["kind"] == "SERVICE_STARTED"
            assert sample["detail"]["service"] == "ibgateway"
            assert sample["detail"]["icon"] == "🟢"

        # Cleanup
        async with AsyncSessionLocal() as session:
            for ev in (ev1, ev2, ev3, ev4):
                to_del = await session.get(EventLogModel, ev.id)
                if to_del:
                    await session.delete(to_del)
            to_del_user = await session.get(UserModel, user.id)
            if to_del_user:
                await session.delete(to_del_user)
            await session.commit()
