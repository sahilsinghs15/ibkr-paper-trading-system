"""Tests for frontend and operator action auditing in event_log."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.models.account import AccountModel
from app.db.models.event import EventLogModel
from app.db.repositories.event_repository import EventRepository
from app.main import app
from app.schemas.reconcile_schemas import FlattenBrokerPositionResponse


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-admin-token"}


@pytest.mark.asyncio
async def test_audit_categories_includes_config_and_kill_switch(session_factory):
    async with session_factory() as session:
        repo = EventRepository(session)
        # Append events
        await repo.append(
            process="kill_switch",
            kind="ENGINE_POSITION_FLATTEN",
            detail={"account_id": 1, "scope": "ENGINE_POSITIONS"},
        )
        await repo.append(
            process="config",
            kind="ACCOUNT_SETTINGS_CHANGED",
            detail={"account_id": 1, "setting": "total_margin"},
        )
        await repo.append(
            process="reconcile",
            kind="FIX_BUTTON_CLICKED",
            detail={"account_id": 1, "target": "POSITION_MISMATCH"},
        )
        await session.commit()

        # Query by risk category (maps kill_switch)
        risk_events, _ = await repo.query_events(category="risk")
        kinds = [e.kind for e in risk_events]
        assert "ENGINE_POSITION_FLATTEN" in kinds

        # Query by config category
        config_events, _ = await repo.query_events(category="config")
        c_kinds = [e.kind for e in config_events]
        assert "ACCOUNT_SETTINGS_CHANGED" in c_kinds

        # Query by reconcile category
        rec_events, _ = await repo.query_events(category="reconcile")
        r_kinds = [e.kind for e in rec_events]
        assert "FIX_BUTTON_CLICKED" in r_kinds


@pytest.mark.asyncio
async def test_patch_account_emits_account_settings_changed(session_factory):
    app.state.session_factory = session_factory
    acc_id = uuid.uuid4().int % 1000000 + 400000
    ibkr_acc = f"DU{acc_id}"
    async with session_factory() as s, s.begin():
        account = AccountModel(
            id=acc_id,
            name="Audit Test Acc",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000"),
            enabled=True,
            default_symbol_limit=Decimal("50000"),
        )
        s.add(account)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.patch(
            f"/api/v1/config/accounts/{acc_id}",
            json={"total_margin": 150000, "enabled": False},
        )
        assert resp.status_code == 200

    # Verify event_log entry
    async with session_factory() as s:
        stmt = (
            select(EventLogModel)
            .where(
                EventLogModel.kind == "ACCOUNT_SETTINGS_CHANGED",
                EventLogModel.detail["account_id"].astext == str(acc_id),
            )
            .order_by(EventLogModel.id.desc())
        )
        row = (await s.execute(stmt)).scalar_one_or_none()
        assert row is not None
        assert row.process == "config"
        assert row.detail["account_id"] == acc_id
        assert row.detail["ibkr_account"] == ibkr_acc
        assert "total_margin" in row.detail["changes"]


@pytest.mark.asyncio
async def test_patch_account_unchanged_does_not_emit_duplicate_audit(session_factory):
    app.state.session_factory = session_factory
    acc_id = uuid.uuid4().int % 1000000 + 500000
    ibkr_acc = f"DU{acc_id}"
    async with session_factory() as s, s.begin():
        account = AccountModel(
            id=acc_id,
            name="Unchanged Acc",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000"),
            enabled=True,
        )
        s.add(account)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        # Patch with identical total_margin (no actual change)
        resp = await ac.patch(
            f"/api/v1/config/accounts/{acc_id}",
            json={"total_margin": 100000},
        )
        assert resp.status_code == 200

    async with session_factory() as s:
        stmt = select(EventLogModel).where(
            EventLogModel.kind == "ACCOUNT_SETTINGS_CHANGED",
            EventLogModel.detail["account_id"].astext == str(acc_id),
        )
        events = (await s.execute(stmt)).scalars().all()
        # No event emitted because nothing changed
        assert len(events) == 0


@pytest.mark.asyncio
async def test_kill_switch_clear_emits_audit_event(session_factory):
    app.state.session_factory = session_factory
    acc_id = uuid.uuid4().int % 1000000 + 600000
    ibkr_acc = f"DU{acc_id}"
    async with session_factory() as s, s.begin():
        account = AccountModel(
            id=acc_id,
            name="KS Clear Acc",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000"),
            enabled=True,
        )
        s.add(account)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post(f"/api/v1/config/accounts/{acc_id}/kill-switch/clear")
        assert resp.status_code == 200

    async with session_factory() as s:
        stmt = select(EventLogModel).where(
            EventLogModel.kind == "KILL_SWITCH_CLEARED",
            EventLogModel.detail["account_id"].astext == str(acc_id),
        )
        event = (await s.execute(stmt)).scalar_one_or_none()
        assert event is not None
        assert event.process == "kill_switch"
        assert event.detail["scope"] == "KILL_SWITCH_CLEARED"


@pytest.mark.asyncio
async def test_positions_align_emits_fix_button_clicked_audit(session_factory):
    app.state.session_factory = session_factory
    acc_id = uuid.uuid4().int % 1000000 + 700000
    ibkr_acc = f"DU{acc_id}"
    async with session_factory() as s, s.begin():
        account = AccountModel(
            id=acc_id,
            name="Align Acc",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000"),
            enabled=True,
        )
        s.add(account)

    mock_response = FlattenBrokerPositionResponse(
        ibkr_account=ibkr_acc,
        account_id=acc_id,
        symbol="IBUS500",
        sec_type="CFD",
        con_id=970001,
        side="BUY",
        quantity=10.0,
        status="ALIGNED",
        success=True,
        message="Broker line aligned to ledger successfully.",
    )

    with patch(
        "app.services.broker_align_service.BrokerAlignService.align_line",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/reconcile/positions/align",
                json={
                    "ibkr_account": ibkr_acc,
                    "symbol": "IBUS500",
                    "sec_type": "CFD",
                    "con_id": 970001,
                },
            )
            assert resp.status_code == 200

    async with session_factory() as s:
        stmt = select(EventLogModel).where(
            EventLogModel.kind == "FIX_BUTTON_CLICKED",
            EventLogModel.detail["ibkr_account"].astext == ibkr_acc,
        )
        event = (await s.execute(stmt)).scalar_one_or_none()
        assert event is not None
        assert event.process == "reconcile"
        assert event.detail["target"] == "POSITION_MISMATCH"
        assert event.detail["symbol"] == "IBUS500"
        assert event.detail["con_id"] == 970001
