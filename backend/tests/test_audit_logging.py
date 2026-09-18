"""Operator actions go to audit_events; event_log stays the machine event journal.

These replace the legacy assertions that operator actions were written to
event_log (the retired "audit logs" view).
"""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.models.account import AccountModel
from app.db.models.audit import AuditEventModel
from app.db.models.event import EventLogModel
from app.db.repositories.event_repository import EventRepository
from app.main import app
from app.schemas.reconcile_schemas import (
    FlattenBrokerPositionResponse,
    ReconcilePositionsResponse,
)


async def _account(session_factory, base: int) -> tuple[int, str]:
    acc_id = uuid.uuid4().int % 1000000 + base
    ibkr_acc = f"DU{acc_id}"
    async with session_factory() as s, s.begin():
        s.add(
            AccountModel(
                id=acc_id,
                name="Audit Test Acc",
                ibkr_account=ibkr_acc,
                total_margin=Decimal(100000),
                enabled=True,
                default_symbol_limit=Decimal(50000),
            )
        )
    return acc_id, ibkr_acc


@pytest.mark.asyncio
async def test_event_journal_categories_still_query_machine_events(session_factory):
    async with session_factory() as session:
        repo = EventRepository(session)
        await repo.append(
            process="kill_switch",
            kind="ENGINE_POSITION_FLATTEN",
            detail={"account_id": 1, "scope": "ENGINE_POSITIONS"},
        )
        await repo.append(
            process="reconcile",
            kind="POSITION_RECONCILE",
            detail={"account_id": 1},
        )
        await session.commit()

        risk_events, _ = await repo.query_events(category="risk")
        assert "ENGINE_POSITION_FLATTEN" in [e.kind for e in risk_events]
        rec_events, _ = await repo.query_events(category="reconcile")
        assert "POSITION_RECONCILE" in [e.kind for e in rec_events]


@pytest.mark.asyncio
async def test_patch_account_writes_audit_event_not_legacy_event_log(session_factory):
    app.state.session_factory = session_factory
    acc_id, ibkr_acc = await _account(session_factory, 400000)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.patch(
            f"/api/v1/config/accounts/{acc_id}",
            json={"total_margin": 150000, "enabled": False},
        )
        assert resp.status_code == 200

    async with session_factory() as s:
        audit = (
            await s.execute(
                select(AuditEventModel).where(
                    AuditEventModel.action == "ACCOUNT_SETTINGS_UPDATED",
                    AuditEventModel.account_id == acc_id,
                )
            )
        ).scalar_one()
        legacy = (
            await s.execute(
                select(EventLogModel).where(
                    EventLogModel.kind == "ACCOUNT_SETTINGS_CHANGED",
                    EventLogModel.detail["account_id"].astext == str(acc_id),
                )
            )
        ).scalars().all()
    assert audit.ibkr_account == ibkr_acc
    assert audit.category == "SETTINGS"
    # pytest bypass principal (no token): identity is the mock admin, flagged as such.
    assert audit.auth_method == "pytest-bypass"
    assert legacy == []


@pytest.mark.asyncio
async def test_patch_account_unchanged_values_are_marked_no_effective_change(session_factory):
    app.state.session_factory = session_factory
    acc_id, _ = await _account(session_factory, 500000)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.patch(f"/api/v1/config/accounts/{acc_id}", json={"total_margin": 100000})
        assert resp.status_code == 200

    async with session_factory() as s:
        audit = (
            await s.execute(
                select(AuditEventModel).where(
                    AuditEventModel.action == "ACCOUNT_SETTINGS_UPDATED",
                    AuditEventModel.account_id == acc_id,
                )
            )
        ).scalar_one()
    assert "no effective change" in audit.summary


@pytest.mark.asyncio
async def test_kill_switch_clear_keeps_journal_event_and_adds_audit(session_factory):
    app.state.session_factory = session_factory
    acc_id, _ = await _account(session_factory, 600000)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(f"/api/v1/config/accounts/{acc_id}/kill-switch/clear")
        assert resp.status_code == 200

    async with session_factory() as s:
        journal = (
            await s.execute(
                select(EventLogModel).where(
                    EventLogModel.kind == "KILL_SWITCH_CLEARED",
                    EventLogModel.detail["account_id"].astext == str(acc_id),
                )
            )
        ).scalar_one_or_none()
        audit = (
            await s.execute(
                select(AuditEventModel).where(
                    AuditEventModel.action == "KILL_SWITCH_CLEARED",
                    AuditEventModel.account_id == acc_id,
                )
            )
        ).scalar_one()
    assert journal is not None and journal.process == "kill_switch"
    assert audit.category == "EMERGENCY"


@pytest.mark.asyncio
async def test_positions_align_is_audited_as_inventory_fix(session_factory):
    app.state.session_factory = session_factory
    acc_id, ibkr_acc = await _account(session_factory, 700000)

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

    with (
        patch(
            "app.services.broker_align_service.BrokerAlignService.align_line",
            new_callable=AsyncMock,
            return_value=mock_response,
        ),
        patch(
            "app.api.routes.reconcile.collect_reconcile_positions",
            new=AsyncMock(return_value=ReconcilePositionsResponse()),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
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
        audit = (
            await s.execute(
                select(AuditEventModel).where(
                    AuditEventModel.category == "INVENTORY",
                    AuditEventModel.ibkr_account == ibkr_acc,
                )
            )
        ).scalar_one()
        legacy = (
            await s.execute(
                select(EventLogModel).where(
                    EventLogModel.kind == "FIX_BUTTON_CLICKED",
                    EventLogModel.detail["ibkr_account"].astext == ibkr_acc,
                )
            )
        ).scalars().all()
    # No current diff for this line -> fix type cannot be classified server-side.
    assert audit.action == "INVENTORY_FIX_UNCLASSIFIED"
    assert audit.target_id == "IBUS500 con_id=970001"
    assert audit.result == "SUCCEEDED"
    assert legacy == []
