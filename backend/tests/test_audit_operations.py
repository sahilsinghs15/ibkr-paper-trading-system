"""Audit coverage of authoritative operator operations.

Settings, inventory fixes, manual trading, positions, emergency controls and
service lifecycle (including the commit-before-restart ordering guarantee).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.audit.security_events import reset_throttle
from app.db.models.audit import AuditEventModel
from app.db.models.strategy import AllocationModel, StrategyModel
from app.main import app
from app.schemas.config_schemas import ClosePairResponse
from app.schemas.manual_schemas import ManualOrderRead, ManualOrderSubmitResponse
from app.schemas.reconcile_schemas import (
    FlattenBrokerPositionResponse,
    ReconcileDiffRow,
    ReconcilePositionsResponse,
)
from tests.audit_test_utils import (
    audit_rows,
    auth_headers,
    client_for,
    create_account,
    create_user,
    install_app_state,
    login,
    single_audit,
)


@pytest.fixture(autouse=True)
def _state(session_factory):
    install_app_state(session_factory)
    reset_throttle()
    yield


@pytest.fixture
async def admin_token(session_factory):
    user = await create_user(session_factory)
    async with client_for() as client:
        token = await login(client, user)
    return user, token


# --------------------------------------------------------------------------- settings


async def test_account_settings_change_records_previous_and_new(session_factory, admin_token):
    user, token = admin_token
    account = await create_account(session_factory)
    async with client_for("198.51.100.7") as client:
        res = await client.patch(
            f"/api/v1/config/accounts/{account.id}",
            json={"total_margin": 150000, "enabled": False},
            headers=auth_headers(token),
        )
    assert res.status_code == 200, res.text
    row = await single_audit(
        session_factory, action="ACCOUNT_SETTINGS_UPDATED", account_id=account.id
    )
    assert row.category == "SETTINGS" and row.result == "SUCCEEDED"
    assert row.ibkr_account == account.ibkr_account
    assert row.target_type == "ACCOUNT" and row.target_id == account.ibkr_account
    assert Decimal(row.before_state["total_margin"]) == Decimal(100000)
    assert Decimal(row.after_state["total_margin"]) == Decimal(150000)
    assert row.before_state["enabled"] is True and row.after_state["enabled"] is False
    assert Decimal(row.parameters["total_margin"]) == Decimal(150000)
    assert row.parameters["enabled"] is False
    assert "total_margin" in row.summary and "enabled" in row.summary
    assert row.actor_user_id == user.id and row.session_id is not None
    assert str(row.client_ip) == "198.51.100.7"


async def test_settings_rejection_is_recorded_and_state_unchanged(session_factory, admin_token):
    _, token = admin_token
    account = await create_account(session_factory)
    async with client_for() as client:
        res = await client.patch(
            f"/api/v1/config/accounts/{account.id}", json={}, headers=auth_headers(token)
        )
    assert res.status_code == 400
    row = await single_audit(
        session_factory, action="ACCOUNT_SETTINGS_UPDATED", account_id=account.id
    )
    assert row.result == "REJECTED"
    assert row.result_reason == "No fields to update."
    assert row.after_state is None


async def test_symbol_limit_set_and_removed(session_factory, admin_token):
    _, token = admin_token
    account = await create_account(session_factory)
    async with client_for() as client:
        put1 = await client.put(
            f"/api/v1/config/accounts/{account.id}/symbol-limits/aapl",
            json={"money_limit": 25000},
            headers=auth_headers(token),
        )
        put2 = await client.put(
            f"/api/v1/config/accounts/{account.id}/symbol-limits/AAPL",
            json={"money_limit": 30000},
            headers=auth_headers(token),
        )
        delete = await client.delete(
            f"/api/v1/config/accounts/{account.id}/symbol-limits/AAPL",
            headers=auth_headers(token),
        )
    assert put1.status_code == 200 and put2.status_code == 200 and delete.status_code == 204
    sets = await audit_rows(session_factory, action="SYMBOL_LIMIT_SET", account_id=account.id)
    assert len(sets) == 2
    assert sets[0].before_state is None
    assert Decimal(sets[0].after_state["money_limit"]) == Decimal(25000)
    assert Decimal(sets[1].before_state["money_limit"]) == Decimal(25000)
    assert Decimal(sets[1].after_state["money_limit"]) == Decimal(30000)
    removed = await single_audit(
        session_factory, action="SYMBOL_LIMIT_REMOVED", account_id=account.id
    )
    assert removed.target_id == "AAPL"
    assert Decimal(removed.before_state["money_limit"]) == Decimal(30000)


async def test_default_symbol_limit_previous_and_new(session_factory, admin_token):
    _, token = admin_token
    account = await create_account(session_factory)
    async with client_for() as client:
        res = await client.put(
            f"/api/v1/config/accounts/{account.id}/default-symbol-limit",
            json={"default_symbol_limit": 75000},
            headers=auth_headers(token),
        )
    assert res.status_code == 200
    row = await single_audit(
        session_factory, action="DEFAULT_SYMBOL_LIMIT_UPDATED", account_id=account.id
    )
    assert Decimal(row.before_state["default_symbol_limit"]) == Decimal(50000)
    assert Decimal(row.after_state["default_symbol_limit"]) == Decimal(75000)


async def test_allocation_update_records_diff(session_factory, admin_token):
    _, token = admin_token
    account = await create_account(session_factory)
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as s, s.begin():
        s.add(
            StrategyModel(
                strategy_id=f"AUDIT_STRAT_{suffix}",
                legs=2,
                expression="CFD",
                max_open_positions=10,
                weight_source="payload",
                enabled=True,
            )
        )
        await s.flush()
        alloc = AllocationModel(
            account_id=account.id,
            strategy_id=f"AUDIT_STRAT_{suffix}",
            alloc_pct=Decimal("0.5"),
            target=Decimal(500),
            stop=Decimal(-250),
            time_limit=3600,
            max_open_positions=5,
            enabled=True,
        )
        s.add(alloc)
        await s.flush()
        alloc_id = alloc.id
    async with client_for() as client:
        res = await client.patch(
            f"/api/v1/config/allocations/{alloc_id}",
            json={"max_open_positions": 7, "enabled": False},
            headers=auth_headers(token),
        )
    assert res.status_code == 200, res.text
    row = await single_audit(session_factory, action="ALLOCATION_UPDATED", account_id=account.id)
    assert row.before_state["max_open_positions"] == 5
    assert row.after_state["max_open_positions"] == 7
    assert row.after_state["enabled"] is False
    assert row.related["allocation_id"] == alloc_id


async def test_execution_and_margin_settings_are_audited(session_factory, admin_token):
    _, token = admin_token
    async with client_for() as client:
        exe = await client.patch(
            "/api/v1/config/execution",
            json={"max_retries": 3},
            headers=auth_headers(token),
        )
        margin = await client.patch(
            "/api/v1/config/margin",
            json={"comfort_ratio": "0.85"},
            headers=auth_headers(token),
        )
    assert exe.status_code == 200, exe.text
    assert margin.status_code == 200, margin.text
    exe_rows = await audit_rows(session_factory, action="EXECUTION_SETTINGS_UPDATED")
    assert exe_rows[-1].after_state["max_retries"] == 3
    assert exe_rows[-1].target_id == "global"
    margin_rows = await audit_rows(session_factory, action="MARGIN_SETTINGS_UPDATED")
    assert Decimal(margin_rows[-1].after_state["comfort_ratio"]) == Decimal("0.85")


async def test_account_create_and_delete_are_audited(session_factory, admin_token):
    _, token = admin_token
    code = f"DUNEW{uuid.uuid4().hex[:6].upper()}"
    async with client_for() as client:
        created = await client.post(
            "/api/v1/config/accounts",
            json={"name": "Audit create", "ibkr_account": code, "total_margin": 1000},
            headers=auth_headers(token),
        )
        assert created.status_code == 201, created.text
        acc_id = created.json()["id"]
        deleted = await client.delete(
            f"/api/v1/config/accounts/{acc_id}", headers=auth_headers(token)
        )
    assert deleted.status_code == 204, deleted.text
    create_row = await single_audit(session_factory, action="ACCOUNT_CREATED", account_id=acc_id)
    assert create_row.category == "ACCOUNT_ADMIN"
    assert create_row.after_state["ibkr_account"] == code
    delete_row = await single_audit(session_factory, action="ACCOUNT_DELETED", account_id=acc_id)
    assert delete_row.before_state["account"]["ibkr_account"] == code


# --------------------------------------------------------------------------- inventory


def _recon(kind: str, *, account_id: int, ibkr: str, broker: float | None, ledger: float | None):
    return ReconcilePositionsResponse(
        diffs=[
            ReconcileDiffRow(
                kind=kind,
                ibkr_account=ibkr,
                account_id=account_id,
                symbol="IBUS500",
                sec_type="CFD",
                con_id=970001,
                broker_qty=broker,
                ledger_qty=ledger,
            )
        ]
    )


@pytest.mark.parametrize(
    ("kind", "broker", "ledger", "action", "side", "qty"),
    [
        ("QTY_DRIFT", 10.0, 6.0, "INVENTORY_FIX_QUANTITY_MISMATCH", "SELL", 4.0),
        ("LEDGER_GHOST", 0.0, 5.0, "INVENTORY_FIX_LEDGER_GHOST", "BUY", 5.0),
        ("BROKER_ORPHAN", -3.0, 0.0, "INVENTORY_FIX_BROKER_GHOST", "BUY", 3.0),
        ("SOMETHING_NEW", 1.0, 2.0, "INVENTORY_FIX_UNCLASSIFIED", "BUY", 1.0),
    ],
)
async def test_inventory_fix_records_exact_fix_type_and_state(
    session_factory, admin_token, kind, broker, ledger, action, side, qty
):
    _, token = admin_token
    account = await create_account(session_factory)
    response = FlattenBrokerPositionResponse(
        ibkr_account=account.ibkr_account,
        account_id=account.id,
        symbol="IBUS500",
        sec_type="CFD",
        con_id=970001,
        side=side,
        quantity=qty,
        status="FLAT",
        success=True,
        message="aligned",
    )
    with (
        patch(
            "app.api.routes.reconcile.collect_reconcile_positions",
            new=AsyncMock(
                return_value=_recon(
                    kind, account_id=account.id, ibkr=account.ibkr_account, broker=broker, ledger=ledger
                )
            ),
        ),
        patch(
            "app.services.broker_align_service.BrokerAlignService.align_line",
            new=AsyncMock(return_value=response),
        ),
    ):
        async with client_for() as client:
            res = await client.post(
                "/api/v1/reconcile/positions/align",
                json={
                    "ibkr_account": account.ibkr_account,
                    "symbol": "IBUS500",
                    "sec_type": "CFD",
                    "con_id": 970001,
                },
                headers=auth_headers(token),
            )
    assert res.status_code == 200, res.text
    row = await single_audit(session_factory, action=action, ibkr_account=account.ibkr_account)
    assert row.category == "INVENTORY"
    assert row.result == "SUCCEEDED"
    assert row.parameters["fix_type"] == kind
    assert row.before_state["diff"]["broker_qty"] == broker
    assert row.before_state["diff"]["ledger_qty"] == ledger
    assert row.after_state["execution"]["side"] == side
    assert row.after_state["execution"]["quantity"] == qty
    assert row.after_state["expected_broker_qty_after_fill"] == pytest.approx(
        broker + (qty if side == "BUY" else -qty)
    )
    assert row.account_id == account.id
    assert "970001" in row.ref_ids


async def test_inventory_fix_rejection_is_recorded(session_factory, admin_token):
    _, token = admin_token
    account = await create_account(session_factory)
    with (
        patch(
            "app.api.routes.reconcile.collect_reconcile_positions",
            new=AsyncMock(
                return_value=_recon(
                    "QTY_DRIFT", account_id=account.id, ibkr=account.ibkr_account, broker=1.0, ledger=2.0
                )
            ),
        ),
        patch(
            "app.services.broker_align_service.BrokerAlignService.align_line",
            new=AsyncMock(
                side_effect=HTTPException(status_code=409, detail="Align already in progress")
            ),
        ),
    ):
        async with client_for() as client:
            res = await client.post(
                "/api/v1/reconcile/positions/align",
                json={
                    "ibkr_account": account.ibkr_account,
                    "symbol": "IBUS500",
                    "sec_type": "CFD",
                    "con_id": 970001,
                },
                headers=auth_headers(token),
            )
    assert res.status_code == 409
    row = await single_audit(
        session_factory,
        action="INVENTORY_FIX_QUANTITY_MISMATCH",
        ibkr_account=account.ibkr_account,
    )
    assert row.result == "REJECTED"
    assert row.result_reason == "Align already in progress"


async def test_broker_line_flatten_is_audited(session_factory, admin_token):
    _, token = admin_token
    account = await create_account(session_factory)
    response = FlattenBrokerPositionResponse(
        ibkr_account=account.ibkr_account,
        account_id=account.id,
        symbol="IBUS500",
        sec_type="CFD",
        con_id=970001,
        side="SELL",
        quantity=2.0,
        status="PARTIAL",
        success=False,
        message="Broker flatten partially filled.",
    )
    with (
        patch(
            "app.api.routes.reconcile.collect_reconcile_positions",
            new=AsyncMock(return_value=ReconcilePositionsResponse()),
        ),
        patch(
            "app.services.broker_flatten_service.BrokerFlattenService.flatten_line",
            new=AsyncMock(return_value=response),
        ),
    ):
        async with client_for() as client:
            res = await client.post(
                "/api/v1/reconcile/positions/flatten",
                json={
                    "ibkr_account": account.ibkr_account,
                    "symbol": "IBUS500",
                    "sec_type": "CFD",
                    "con_id": 970001,
                    "quantity": 2,
                },
                headers=auth_headers(token),
            )
    assert res.status_code == 200
    row = await single_audit(
        session_factory,
        action="INVENTORY_BROKER_LINE_FLATTEN",
        ibkr_account=account.ibkr_account,
    )
    assert row.result == "PARTIAL"
    assert row.parameters["quantity"] == 2.0
    assert row.parameters["fix_type"] == "BROKER_LINE_FLATTEN"


# --------------------------------------------------------------------------- manual trading


def _manual_order(account, *, internal_id: str) -> ManualOrderRead:
    now = datetime.now(UTC)
    return ManualOrderRead(
        id=4242,
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        idempotency_key="idem-1",
        internal_order_id=internal_id,
        trade_id="TRD_AUDIT",
        broker_order_id="881",
        perm_id=99001,
        con_id=265598,
        symbol="AAPL",
        sec_type="CFD",
        exchange="SMART",
        currency="USD",
        side="BUY",
        quantity=Decimal(10),
        order_type="LIMIT",
        limit_price=Decimal("190.5"),
        tif="DAY",
        outside_rth=False,
        status="SUBMITTED",
        created_at=now,
        updated_at=now,
    )


_ORDER_BODY = {
    "idempotency_key": "idem-1",
    "symbol": "AAPL",
    "con_id": 265598,
    "side": "BUY",
    "quantity": "10",
    "order_type": "LIMIT",
    "limit_price": "190.5",
}


async def test_manual_order_submit_success_captures_parameters_and_ids(
    session_factory, admin_token
):
    user, token = admin_token
    account = await create_account(session_factory)
    internal_id = f"MAN_{uuid.uuid4().hex[:12].upper()}"
    resp = ManualOrderSubmitResponse(
        order=_manual_order(account, internal_id=internal_id), message="placed"
    )
    with patch(
        "app.api.routes.manual.ManualTradingService.submit_order",
        new=AsyncMock(return_value=resp),
    ):
        async with client_for("198.51.100.77") as client:
            res = await client.post(
                f"/api/v1/manual/orders?ibkr_account={account.ibkr_account}",
                json=_ORDER_BODY,
                headers=auth_headers(token),
            )
    assert res.status_code == 200, res.text
    row = await single_audit(session_factory, action="MANUAL_ORDER_SUBMIT", account_id=account.id)
    assert row.category == "MANUAL_TRADING" and row.result == "SUCCEEDED"
    assert row.parameters["side"] == "BUY"
    assert row.parameters["quantity"] == "10"
    assert row.parameters["order_type"] == "LIMIT"
    assert row.parameters["limit_price"] == "190.5"
    assert row.parameters["con_id"] == 265598
    assert row.target_type == "MANUAL_ORDER" and row.target_id == internal_id
    assert row.related["internal_order_id"] == internal_id
    assert row.related["broker_order_id"] == "881"
    assert internal_id in row.ref_ids and "99001" in row.ref_ids
    assert row.after_state["order"]["status"] == "SUBMITTED"
    assert row.actor_user_id == user.id
    assert str(row.client_ip) == "198.51.100.77"
    assert row.completed_at is not None and row.completed_at >= row.occurred_at


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (400, "Order validation failed: quantity below minimum", "REJECTED"),
        (409, "This order request has already been used with different parameters.", "REJECTED"),
        (503, "IBKR Gateway is disconnected.", "FAILED"),
    ],
)
async def test_manual_order_rejection_and_failure(session_factory, admin_token, status, detail, expected):
    _, token = admin_token
    account = await create_account(session_factory)
    with patch(
        "app.api.routes.manual.ManualTradingService.submit_order",
        new=AsyncMock(side_effect=HTTPException(status_code=status, detail=detail)),
    ):
        async with client_for() as client:
            res = await client.post(
                f"/api/v1/manual/orders?ibkr_account={account.ibkr_account}",
                json=_ORDER_BODY,
                headers=auth_headers(token),
            )
    assert res.status_code == status
    row = await single_audit(session_factory, action="MANUAL_ORDER_SUBMIT", account_id=account.id)
    assert row.result == expected
    assert row.result_reason == detail


async def test_manual_order_idempotent_replay_is_explicit(session_factory, admin_token):
    _, token = admin_token
    account = await create_account(session_factory)
    resp = ManualOrderSubmitResponse(
        order=_manual_order(account, internal_id="MAN_REPLAY0001"),
        idempotent_replay=True,
        message="replay",
    )
    with patch(
        "app.api.routes.manual.ManualTradingService.submit_order",
        new=AsyncMock(return_value=resp),
    ):
        async with client_for() as client:
            res = await client.post(
                f"/api/v1/manual/orders?ibkr_account={account.ibkr_account}",
                json=_ORDER_BODY,
                headers=auth_headers(token),
            )
    assert res.status_code == 200
    row = await single_audit(session_factory, action="MANUAL_ORDER_SUBMIT", account_id=account.id)
    assert row.after_state["idempotent_replay"] is True
    assert "Idempotent replay" in (row.result_reason or "")


# --------------------------------------------------------------------------- positions


@pytest.mark.parametrize(
    ("status", "success", "expected"),
    [("CLOSED", True, "SUCCEEDED"), ("PARTIAL", False, "PARTIAL"), ("FAILED", False, "FAILED")],
)
async def test_close_pair_is_audited_with_order_ids(
    session_factory, admin_token, status, success, expected
):
    _, token = admin_token
    account = await create_account(session_factory)
    trade_id = f"MBG-{uuid.uuid4().hex[:8]}"
    response = ClosePairResponse(
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        trade_id=trade_id,
        leg_a_symbol="AAPL",
        leg_b_symbol="MSFT",
        status=status,
        success=success,
        message=f"state {status}",
        order_ids=["ORD-A1", "ORD-B1"],
    )
    with patch(
        "app.services.position_close_service.SinglePairCloseService.close_pair",
        new=AsyncMock(return_value=response),
    ):
        async with client_for() as client:
            res = await client.post(
                f"/api/v1/config/accounts/{account.id}/positions/{trade_id}/close",
                headers=auth_headers(token),
            )
    assert res.status_code == 200, res.text
    row = await single_audit(session_factory, action="CLOSE_PAIR", target_id=trade_id)
    assert row.category == "POSITIONS"
    assert row.result == expected
    assert row.account_id == account.id and row.ibkr_account == account.ibkr_account
    assert row.related["internal_order_ids"] == ["ORD-A1", "ORD-B1"]
    assert {"ORD-A1", "ORD-B1", trade_id} <= set(row.ref_ids)
    assert row.after_state["status"] == status


# --------------------------------------------------------------------------- emergency


async def test_emergency_actions_are_distinguishable(session_factory, admin_token):
    _, token = admin_token
    app.state.order_manager = None  # real KillSwitchService, no execution engine
    engine_acc = await create_account(session_factory)
    manual_acc = await create_account(session_factory)
    full_acc = await create_account(session_factory)
    flatten_result = {
        "success": True,
        "submitted": 2,
        "filled": 2,
        "rejected": 0,
        "pending": 0,
        "positions_found": 2,
        "error": None,
    }
    with (
        patch(
            "app.services.kill_switch.KillSwitchService.execute_flatten_operation_background",
            new=AsyncMock(),
        ),
        patch(
            "app.services.kill_switch.KillSwitchService.execute_manual_flatten_operation_background",
            new=AsyncMock(),
        ),
        patch(
            "app.services.kill_switch.KillSwitchService.close_all_ledger_after_account_flatten",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "scripts.oms.flatten_gateway_positions.run_flatten_gateway_positions",
            return_value=flatten_result,
        ),
    ):
        async with client_for() as client:
            r1 = await client.post(
                f"/api/v1/config/accounts/{engine_acc.id}/square-off?scope=engine",
                headers=auth_headers(token),
            )
            r2 = await client.post(
                f"/api/v1/config/accounts/{manual_acc.id}/square-off-manual",
                headers=auth_headers(token),
            )
            r3 = await client.post(
                f"/api/v1/config/accounts/{full_acc.id}/square-off-account",
                headers=auth_headers(token),
            )
            r4 = await client.post(
                f"/api/v1/config/accounts/{engine_acc.id}/kill-switch/clear",
                headers=auth_headers(token),
            )
    for r in (r1, r2, r3, r4):
        assert r.status_code in (200, 202), r.text

    kill = await single_audit(
        session_factory, action="KILL_SWITCH_ENGINE_FLATTEN", account_id=engine_acc.id
    )
    kill_manual = await single_audit(
        session_factory, action="KILL_MANUAL_FLATTEN", account_id=manual_acc.id
    )
    full = await single_audit(
        session_factory, action="COMPLETE_ACCOUNT_FLATTEN", account_id=full_acc.id
    )
    cleared = await single_audit(
        session_factory, action="KILL_SWITCH_CLEARED", account_id=engine_acc.id
    )
    assert {kill.category, kill_manual.category, full.category, cleared.category} == {"EMERGENCY"}
    assert kill.result == "ACCEPTED" and kill_manual.result == "ACCEPTED"
    assert kill.related["operation_id"] == r1.json()["operation_id"]
    assert kill_manual.related["operation_id"] == r2.json()["operation_id"]
    assert kill.after_state["kill_switch_active"] is True
    assert full.result == "SUCCEEDED"
    assert full.after_state["broker_orders_submitted"] == 2
    assert full.related["operation_id"] == r3.json()["operation_id"]
    assert cleared.before_state["kill_switch_active"] is True
    assert cleared.after_state["kill_switch_active"] is False

    async with client_for() as client:
        listing = await client.get(
            "/api/v1/audit/events",
            params={"category": "EMERGENCY", "action": "KILL_MANUAL_FLATTEN", "account": manual_acc.ibkr_account},
            headers=auth_headers(token),
        )
    assert listing.status_code == 200
    assert [i["action"] for i in listing.json()["items"]] == ["KILL_MANUAL_FLATTEN"]


async def test_trading_pause_and_resume_are_audited_with_real_actor(session_factory, admin_token):
    user, token = admin_token
    account = await create_account(session_factory)
    async with client_for() as client:
        paused = await client.post(
            f"/api/v1/config/accounts/{account.id}/trading-pause", headers=auth_headers(token)
        )
        resumed = await client.post(
            f"/api/v1/config/accounts/{account.id}/trading-pause/clear",
            headers=auth_headers(token),
        )
    assert paused.status_code == 200 and resumed.status_code == 200
    assert paused.json()["paused_by"] == user.email
    pause_row = await single_audit(session_factory, action="TRADING_PAUSED", account_id=account.id)
    assert pause_row.before_state["trading_paused"] is False
    assert pause_row.after_state["trading_paused"] is True
    resume_row = await single_audit(session_factory, action="TRADING_RESUMED", account_id=account.id)
    assert resume_row.after_state["trading_paused"] is False


async def test_external_emergency_webhook_is_a_service_actor(session_factory):
    from app.core.config import Settings

    app.state.order_manager = None
    account = await create_account(session_factory)
    secret = f"secret-{uuid.uuid4().hex[:6]}"
    with patch(
        "app.api.routes.emergency.get_settings",
        return_value=Settings(emergency_killswitch_auth_secret=secret),
    ):
        async with client_for("192.0.2.200") as client:
            res = await client.post(
                "/api/v1/emergency-kill-switch",
                headers={"Authorization": f"Bearer {secret}"},
                json={"ibkr_account_id": account.ibkr_account},
            )
    assert res.status_code == 200
    row = await single_audit(
        session_factory, action="KILL_SWITCH_ARMED_EXTERNAL", account_id=account.id
    )
    assert row.actor_type == "SERVICE"
    assert row.actor_user_id is None and row.actor_email is None
    assert row.auth_method == "shared-secret"
    assert row.context["identity"]["service_label"] == "emergency_webhook"
    assert str(row.client_ip) == "192.0.2.200"
    assert secret not in str(row.context) and secret not in str(row.parameters)


# --------------------------------------------------------------------------- system control


def _completed(cmd, returncode=0):
    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout="", stderr="")


def _systemctl_stub(calls: list[list[str]], *, fail_action: str | None = None):
    def run(cmd, **_kwargs):
        calls.append(list(cmd))
        if cmd[1] == "show":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="ActiveState=active\nSubState=running\nMainPID=4242\n", stderr=""
            )
        if fail_action and fail_action in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Job failed")
        return _completed(cmd)

    return run


@pytest.mark.parametrize(
    ("action", "expected_action"),
    [("start", "SERVICE_START"), ("stop", "SERVICE_STOP"), ("restart", "SERVICE_RESTART")],
)
async def test_service_lifecycle_actions_record_affected_service(
    session_factory, admin_token, action, expected_action
):
    _, token = admin_token
    calls: list[list[str]] = []
    marker = datetime.now(UTC)
    with patch("app.api.routes.service_control.subprocess.run", side_effect=_systemctl_stub(calls)):
        async with client_for() as client:
            res = await client.post(
                f"/api/v1/service-control/webhook/{action}", headers=auth_headers(token)
            )
    assert res.status_code == 200, res.text
    assert ["systemctl", action, "webhook-ingest.service"] in calls
    async with session_factory() as s:
        row = (
            await s.execute(
                select(AuditEventModel)
                .where(
                    AuditEventModel.action == expected_action,
                    AuditEventModel.target_id == "webhook-ingest.service",
                    AuditEventModel.occurred_at >= marker,
                )
            )
        ).scalar_one()
    assert row.category == "SYSTEM_CONTROL"
    assert row.result == "SUCCEEDED"
    assert row.parameters == {"service": "webhook", "unit": "webhook-ingest.service", "action": action}
    assert row.before_state["ActiveState"] == "active"


@pytest.mark.parametrize("action", ["stop", "restart"])
async def test_trading_backend_audit_is_committed_before_systemctl(
    session_factory, admin_token, action
):
    """The PENDING audit row must be durable (visible to another connection)
    before systemctl is invoked for the unit hosting this process."""
    _, token = admin_token
    loop = asyncio.get_running_loop()
    marker = datetime.now(UTC)
    observed: dict[str, object] = {}

    async def _committed_rows() -> list[tuple[str, str]]:
        # Fresh session/connection: only committed data is visible.
        async with session_factory() as s:
            rows = await s.execute(
                select(AuditEventModel.result, AuditEventModel.action).where(
                    AuditEventModel.target_id == "trading-backend.service",
                    AuditEventModel.occurred_at >= marker,
                )
            )
            return [(r[0], r[1]) for r in rows.all()]

    def run(cmd, **_kwargs):
        if cmd[1] == "show":
            return subprocess.CompletedProcess(cmd, 0, stdout="ActiveState=active\n", stderr="")
        # Runs in a worker thread: query the DB on the event loop and wait.
        observed["cmd"] = list(cmd)
        observed["rows_at_invoke"] = asyncio.run_coroutine_threadsafe(
            _committed_rows(), loop
        ).result(timeout=10)
        return _completed(cmd)

    with patch("app.api.routes.service_control.subprocess.run", side_effect=run):
        async with client_for() as client:
            res = await client.post(
                f"/api/v1/service-control/backend/{action}", headers=auth_headers(token)
            )
    assert res.status_code == 200, res.text
    assert observed["cmd"] == ["systemctl", "--no-block", action, "trading-backend.service"]
    expected = "SERVICE_STOP" if action == "stop" else "SERVICE_RESTART"
    assert observed["rows_at_invoke"] == [("PENDING", expected)]

    final = await _committed_rows()
    assert final == [("ACCEPTED", expected)]


async def test_lifecycle_not_executed_when_audit_cannot_be_persisted(session_factory, admin_token):
    _, token = admin_token
    calls: list[list[str]] = []
    with (
        patch("app.api.routes.service_control.subprocess.run", side_effect=_systemctl_stub(calls)),
        patch("app.audit.recorder.AuditRecorder.record_committed", new=AsyncMock(return_value=None)),
    ):
        async with client_for() as client:
            res = await client.post(
                "/api/v1/service-control/backend/restart", headers=auth_headers(token)
            )
    assert res.status_code == 503
    assert not any(c[1] in ("restart", "--no-block") for c in calls)


async def test_service_control_failure_is_recorded(session_factory, admin_token):
    _, token = admin_token
    calls: list[list[str]] = []
    marker = datetime.now(UTC)
    with patch(
        "app.api.routes.service_control.subprocess.run",
        side_effect=_systemctl_stub(calls, fail_action="start"),
    ):
        async with client_for() as client:
            res = await client.post(
                "/api/v1/service-control/watchdog/start", headers=auth_headers(token)
            )
    assert res.status_code == 500
    async with session_factory() as s:
        row = (
            await s.execute(
                select(AuditEventModel).where(
                    AuditEventModel.action == "SERVICE_START",
                    AuditEventModel.target_id == "watchdog.service",
                    AuditEventModel.occurred_at >= marker,
                )
            )
        ).scalar_one()
    assert row.result == "FAILED"
    assert "Job failed" in (row.result_reason or "")


async def test_service_status_is_not_audited(session_factory, admin_token):
    _, token = admin_token
    marker = datetime.now(UTC)
    with patch(
        "app.api.routes.service_control.subprocess.run", side_effect=_systemctl_stub([])
    ):
        async with client_for() as client:
            res = await client.post(
                "/api/v1/service-control/webhook/status", headers=auth_headers(token)
            )
    assert res.status_code == 200
    async with session_factory() as s:
        n = (
            await s.execute(
                select(AuditEventModel).where(
                    AuditEventModel.category == "SYSTEM_CONTROL",
                    AuditEventModel.occurred_at >= marker,
                )
            )
        ).scalars().all()
    assert n == []
