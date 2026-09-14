"""Tests for system lifecycle and market closed event persistence in PostgreSQL event_log."""

from datetime import UTC, datetime

import pytest

from app.db.repositories.event_repository import EventRepository
from app.db.session import AsyncSessionLocal


@pytest.mark.asyncio
class TestSystemEventPersistence:
    """Validate persistence of SERVICE_STARTED, SERVICE_STOPPED, and MARKET_CLOSED events."""

    async def test_service_started_persistence_and_idempotency(self):
        async with AsyncSessionLocal() as session:
            repo = EventRepository(session)
            idem_key = f"service:ibgateway:start:test_suite_{int(datetime.now(UTC).timestamp())}"
            detail = {
                "service": "ibgateway",
                "unit": "ibgateway.service",
                "action": "start",
                "icon": "🟢",
                "message": "ibgateway started",
            }

            # 1. Insert event
            row1 = await repo.append(
                process="systemd",
                kind="SERVICE_STARTED",
                detail=detail,
                idempotency_key=idem_key,
            )
            await session.commit()
            assert row1 is not None
            assert row1.id > 0
            assert row1.kind == "SERVICE_STARTED"
            assert row1.process == "systemd"
            assert row1.detail["service"] == "ibgateway"
            assert row1.detail["icon"] == "🟢"

            # 2. Duplicate execution with same idempotency key returns same row without duplicate
            row2 = await repo.append(
                process="systemd",
                kind="SERVICE_STARTED",
                detail=detail,
                idempotency_key=idem_key,
            )
            await session.commit()
            assert row2 is not None
            assert row2.id == row1.id

            # Clean up test row
            await session.delete(row1)
            await session.commit()

    async def test_service_stopped_persistence(self):
        async with AsyncSessionLocal() as session:
            repo = EventRepository(session)
            idem_key = f"service:trading-backend:stop:test_suite_{int(datetime.now(UTC).timestamp())}"
            detail = {
                "service": "trading-backend",
                "unit": "trading-backend.service",
                "action": "stop",
                "icon": "🔴",
                "message": "trading-backend stopped",
            }

            row = await repo.append(
                process="systemd",
                kind="SERVICE_STOPPED",
                detail=detail,
                idempotency_key=idem_key,
            )
            await session.commit()
            assert row is not None
            assert row.kind == "SERVICE_STOPPED"
            assert row.detail["service"] == "trading-backend"
            assert row.detail["icon"] == "🔴"

            # Clean up test row
            await session.delete(row)
            await session.commit()

    async def test_market_closed_persistence_and_idempotency(self):
        async with AsyncSessionLocal() as session:
            repo = EventRepository(session)
            date_str = "2026-11-26"
            idem_key = f"market_closed:{date_str}_test"
            detail = {
                "date": date_str,
                "reason": "Thanksgiving",
                "icon": "📅",
                "message": "Market closed — Thanksgiving",
            }

            row1 = await repo.append(
                process="session_clock",
                kind="MARKET_CLOSED",
                detail=detail,
                idempotency_key=idem_key,
            )
            await session.commit()
            assert row1 is not None
            assert row1.kind == "MARKET_CLOSED"
            assert row1.detail["reason"] == "Thanksgiving"
            assert row1.detail["icon"] == "📅"

            # Duplicate
            row2 = await repo.append(
                process="session_clock",
                kind="MARKET_CLOSED",
                detail=detail,
                idempotency_key=idem_key,
            )
            await session.commit()
            assert row2.id == row1.id  # pyrefly: ignore[missing-attribute]

            # Clean up test row
            await session.delete(row1)
            await session.commit()
