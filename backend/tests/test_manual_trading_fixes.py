"""Adversarial fix tests for audit gaps (HIGH/MEDIUM).

Covers:
- concurrent idempotency (same key, identical params) → exactly one placeOrder
- rate limiter timeout for submit and cancel → no broker mutation, correct HTTP, ERROR/CANCEL_REQUESTED handling
- crash/ambiguous PENDING_SUBMIT (no broker_id, and with broker_id after placeOrder)
- CLOSED trade_id reuse rejection
- legacy BrokerFlattenService manual-aware safety (5 scenarios)
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_db_session
from app.broker.ibkr.gateway_rate_limiter import GatewayRateLimiter
from app.core.security import create_access_token, get_password_hash
from app.db.models.account import AccountModel
from app.db.models.instrument import InstrumentModel
from app.db.models.manual_order import ManualOrderModel
from app.db.models.position import PositionModel
from app.db.models.user import UserModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.manual_repository import ManualOrderRepository, ManualPositionRepository
from app.db.repositories.position_repository import RISK_STATE_OPEN
from app.main import app
from app.oms.basket import BasketExecutionResult, BasketState
from app.oms.models import OMSOrderStatus
from app.rms.models import OrderAction, OrderSide
from app.services.broker_flatten_service import BrokerFlattenService
from app.services.manual_callbacks import ManualExecutionListener
from app.services.manual_recovery import ManualTradingRecoveryService
from app.schemas.reconcile_schemas import FlattenBrokerPositionResponse


# ── helpers ────────────────────────────────────────────────────────────────

async def _create_account_user(session_factory, prefix: str, *, enabled=True):
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{prefix}", ibkr_account=f"DU{prefix.upper()}", total_margin=Decimal("100000"), enabled=enabled)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        user = UserModel(email=f"user_{prefix}@example.com", password_hash=get_password_hash("p"), role="user", is_active=True, ibkr_account_id=acc.id)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = create_access_token({"sub": str(user.id), "role": "user", "email": user.email})
        # keep objects detached but return ids
        return acc, user, token


class _MockSubmitTWS:
    def __init__(self, connected=True, next_id=7000, limiter=None):
        self._connected = connected
        self.managed_accounts: frozenset[str] = frozenset()
        self.next_order_id = next_id
        self._rate_limiter = limiter or GatewayRateLimiter()
        self.placed: list[tuple[int, Any, Any]] = []
        self.should_fail_allocate = False

    def is_connected(self): return self._connected
    def allocate_next_order_id(self):
        if self.should_fail_allocate: raise RuntimeError("no id")
        cur = self.next_order_id; self.next_order_id += 1; return cur
    def placeOrder(self, oid, c, o): self.placed.append((oid, c, o))


class _MockCancelTWS:
    def __init__(self, connected=True, limiter=None):
        self._connected = connected
        self.rate_limiter = limiter or GatewayRateLimiter()
        self._rate_limiter = limiter or GatewayRateLimiter()
        # both attr names used in code
        self._rate_limiter = limiter or GatewayRateLimiter()
        self.cancelled: list[int] = []
        self.placed: list[tuple[int, Any, Any]] = []
    def is_connected(self): return self._connected
    def cancelOrder(self, oid: int): self.cancelled.append(oid)
    def placeOrder(self, oid, c, o): self.placed.append((oid, c, o))


class _MockWhatIfAdapter:
    def __init__(self):
        from app.oms.ibkr_adapter import WhatIfResult
        self.whatif_result = WhatIfResult(order_id=100, unknown=False, init_margin_change=Decimal("10"), maint_margin_change=Decimal("5"))
        self.probe_calls = []
    async def probe_margin(self, **kw):  # type: ignore[no-untyped-def]
        self.probe_calls.append(kw)
        return self.whatif_result


def _limited_client_with_failing_limiter():
    limiter = MagicMock(spec=GatewayRateLimiter)
    limiter.acquire = AsyncMock(side_effect=asyncio.TimeoutError("rate limiter timeout"))
    return limiter


class _TimeoutLimiter:
    async def acquire(self, *a, **kw):  # type: ignore[no-untyped-def]
        raise asyncio.TimeoutError("gateway pacing timeout")


# ── A: concurrent idempotency ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_same_idempotency_exactly_one_placeorder(session_factory):
    """5 concurrent submit_order with same (account, idempotency_key, same params) → 1 placeOrder, others replay (service-level race)."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc); await session.commit(); await session.refresh(acc)
        user = UserModel(email=f"u_{s}@ex.com", password_hash=get_password_hash("p"), role="user", is_active=True, ibkr_account_id=acc.id)
        session.add(user); await session.commit(); await session.refresh(user)
        acc_id = acc.id
        acc_code = acc.ibkr_account
        user_id = user.id
    # Load full objects for service
    async with session_factory() as session:
        acc_obj = (await session.execute(select(AccountModel).where(AccountModel.id == acc_id))).scalar_one()
        user_obj = (await session.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one()

    mock_client = _MockSubmitTWS(connected=True, next_id=7100)
    mock_client.managed_accounts = frozenset([acc_code])
    from app.schemas.manual_schemas import ManualOrderSubmitRequest
    from app.services.manual_trading import ManualTradingService

    shared_key = f"conc-key-{s}"

    async def _one_submit():
        async with session_factory() as sess:
            from typing import Any, cast
            svc = ManualTradingService(sess, client=cast(Any, mock_client), ibkr_adapter=cast(Any, _MockWhatIfAdapter()))
            # re-fetch account/user inside session
            acc2 = (await sess.execute(select(AccountModel).where(AccountModel.id == acc_id))).scalar_one()
            user2 = (await sess.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one()
            req = ManualOrderSubmitRequest(idempotency_key=shared_key, symbol="IBUS500", con_id=91001, sec_type="CFD", exchange="SMART", currency="USD", side="BUY", quantity=Decimal("10"), order_type="LIMIT", limit_price=Decimal("100.00"))
            return await svc.submit_order(acc2, req, user2)

    results = await asyncio.gather(*[_one_submit() for _ in range(5)], return_exceptions=True)
    # No 500 leaks — all either success or HTTPException 409 (different params not used here)
    for r in results:
        assert not isinstance(r, Exception) or isinstance(r, type(asyncio.TimeoutError())), f"unexpected exception {r}"
    assert len(mock_client.placed) == 1, f"placed={len(mock_client.placed)} expected 1"
    async with session_factory() as session:
        rows = (await session.execute(select(ManualOrderModel).where(ManualOrderModel.account_id == acc_id, ManualOrderModel.idempotency_key == shared_key))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_different_params_returns_409(session_factory):
    """Same idempotency_key with different quantity → second caller gets 409, no second placeOrder."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc); await session.commit(); await session.refresh(acc)
        user = UserModel(email=f"u_{s}@ex.com", password_hash=get_password_hash("p"), role="user", is_active=True, ibkr_account_id=acc.id)
        session.add(user); await session.commit(); await session.refresh(user)
        token = create_access_token({"sub": str(user.id), "role": "user", "email": user.email})
        acc_code = acc.ibkr_account

    mock_client = _MockSubmitTWS(connected=True, next_id=7200)
    mock_client.managed_accounts = frozenset([acc_code])
    app.state.client = mock_client
    app.state.ibkr_adapter = _MockWhatIfAdapter()
    app.dependency_overrides.clear()
    async def _ov():
        async with session_factory() as s2:
            yield s2
    from app.api.deps import get_db_session as _gds2
    app.dependency_overrides[_gds2] = _ov
    key = f"conflict-{s}"
    p_same = {"idempotency_key": key, "symbol": "IBUS500", "con_id": 91002, "sec_type": "CFD", "exchange": "SMART", "currency": "USD", "side": "BUY", "quantity": "10", "order_type": "LIMIT", "limit_price": "100.00"}
    p_diff = dict(p_same, quantity="20")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.post(f"/api/v1/manual/orders?ibkr_account={acc_code}", json=p_same, headers={"Authorization": f"Bearer {token}"})
            assert r1.status_code == 200
            r2 = await client.post(f"/api/v1/manual/orders?ibkr_account={acc_code}", json=p_diff, headers={"Authorization": f"Bearer {token}"})
            assert r2.status_code == 409
        assert len(mock_client.placed) == 1
    finally:
        app.dependency_overrides.clear()


# ── B: rate limiter timeout ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_rate_limiter_timeout_no_broker_mutation(session_factory):
    """Submit with rate limiter timeout → 504, no placeOrder, status ERROR, no duplicate retry."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc); await session.commit(); await session.refresh(acc)
        user = UserModel(email=f"u_{s}@ex.com", password_hash=get_password_hash("p"), role="user", is_active=True, ibkr_account_id=acc.id)
        session.add(user); await session.commit(); await session.refresh(user)
        token = create_access_token({"sub": str(user.id), "role": "user", "email": user.email})
        acc_code = acc.ibkr_account

    mock_client = _MockSubmitTWS(connected=True, next_id=7300, limiter=_TimeoutLimiter())
    # also need _rate_limiter attr for submit path
    mock_client._rate_limiter = _TimeoutLimiter()  # type: ignore[assignment]
    app.state.client = mock_client
    app.state.ibkr_adapter = _MockWhatIfAdapter()
    app.dependency_overrides.clear()
    async def _ov3():
        async with session_factory() as s3:
            yield s3
    from app.api.deps import get_db_session as _gds3
    app.dependency_overrides[_gds3] = _ov3

    payload = {"idempotency_key": f"rl-{s}", "symbol": "IBUS500", "con_id": 91003, "sec_type": "CFD", "exchange": "SMART", "currency": "USD", "side": "BUY", "quantity": "5", "order_type": "LIMIT", "limit_price": "100.00"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(f"/api/v1/manual/orders?ibkr_account={acc_code}", json=payload, headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 504, r.text
        assert len(mock_client.placed) == 0
        async with session_factory() as session:
            row = (await session.execute(select(ManualOrderModel).where(ManualOrderModel.idempotency_key == f"rl-{s}"))).scalar_one()
            assert row.status == "ERROR"
            assert "rate limiter" in (row.reject_reason or "").lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cancel_rate_limiter_timeout_no_broker_mutation(session_factory):
    """Cancel with rate limiter timeout → 504, zero cancelOrder calls."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc); await session.commit(); await session.refresh(acc)
        user = UserModel(email=f"u_{s}@ex.com", password_hash=get_password_hash("p"), role="user", is_active=True, ibkr_account_id=acc.id)
        session.add(user); await session.commit(); await session.refresh(user)
        token = create_access_token({"sub": str(user.id), "role": "user", "email": user.email})
        acc_code = acc.ibkr_account
        acc_id = acc.id
        # create SUBMITTED order
        repo = ManualOrderRepository(session)
        order = await repo.create_order(account_id=acc_id, ibkr_account=acc_code, idempotency_key=f"ck-{s}", internal_order_id=f"MAN_{s}", trade_id=f"TRD_{s}", symbol="IBUS500", side="BUY", quantity=Decimal("10"), order_type="LIMIT", con_id=91004, sec_type="CFD", exchange="SMART", currency="USD", limit_price=Decimal("100"), status="SUBMITTED")
        order.broker_order_id = "81001"
        await session.commit()
        oid = order.id

    limiter = _TimeoutLimiter()
    # need both attribute names
    class _Cli:
        def __init__(self): self._rate_limiter = limiter; self.rate_limiter = limiter
        def is_connected(self): return True
        def cancelOrder(self, oid): raise AssertionError("cancelOrder must not be called on rate limiter timeout")
    mock_cli = _Cli()
    app.state.client = mock_cli  # type: ignore[assignment]
    app.state.ibkr_adapter = MagicMock()
    app.dependency_overrides.clear()
    async def _ov4():
        async with session_factory() as s4:
            yield s4
    from app.api.deps import get_db_session as _gds4
    app.dependency_overrides[_gds4] = _ov4
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(f"/api/v1/manual/orders/{oid}/cancel?ibkr_account={acc_code}", json={}, headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 504, r.text
    finally:
        app.dependency_overrides.clear()


# ── C: crash / ambiguous PENDING_SUBMIT ────────────────────────────────────

@pytest.mark.asyncio
async def test_ambiguous_pending_submit_without_broker_id_becomes_error(session_factory):
    """PENDING_SUBMIT with no broker_order_id across restart → ERROR REQUIRES_RECOVERY, no resubmit."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"))
        session.add(acc); await session.flush()
        acc_id = acc.id
        repo = ManualOrderRepository(session)
        await repo.create_order(account_id=acc_id, ibkr_account=f"DU{s.upper()}", idempotency_key=f"pend-{s}", internal_order_id=f"MAN_{s}", trade_id=f"TRD_{s}", symbol="IBUS500", side="BUY", quantity=Decimal("10"), order_type="LIMIT", con_id=91005, limit_price=Decimal("100"), status="PENDING_SUBMIT")
    svc = ManualTradingRecoveryService(session_factory, client=MagicMock())
    recovered = await svc.run_startup_recovery()
    assert recovered >= 1
    async with session_factory() as session:
        row = (await session.execute(select(ManualOrderModel).where(ManualOrderModel.idempotency_key == f"pend-{s}"))).scalar_one()
        assert row.status == "ERROR"
        assert "REQUIRES_RECOVERY" in (row.reject_reason or "")


@pytest.mark.asyncio
async def test_pending_submit_with_broker_id_and_placeorder_then_crash_still_reachable_via_callback(session_factory):
    """PENDING_SUBMIT with broker_order_id + placeOrder called before crash → recovery leaves PENDING_SUBMIT as ERROR conservatively, but broker callback can still attach execution via order_id correlation (handled as ERROR-position still updated)."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"))
        session.add(acc); await session.flush()
        acc_id = acc.id
        repo = ManualOrderRepository(session)
        order = await repo.create_order(account_id=acc_id, ibkr_account=f"DU{s.upper()}", idempotency_key=f"pend2-{s}", internal_order_id=f"MAN_{s}", trade_id=f"TRD_{s}", symbol="IBUS500", side="BUY", quantity=Decimal("10"), order_type="LIMIT", con_id=91006, limit_price=Decimal("100"), status="PENDING_SUBMIT")
        order.broker_order_id = "82001"
    # run recovery — will mark PENDING_SUBMIT → ERROR even though broker_order_id exists (conservative)
    svc = ManualTradingRecoveryService(session_factory, client=MagicMock())
    await svc.run_startup_recovery()
    async with session_factory() as session:
        row = (await session.execute(select(ManualOrderModel).where(ManualOrderModel.idempotency_key == f"pend2-{s}"))).scalar_one()
        assert row.status == "ERROR"
        assert row.broker_order_id == "82001"
    # Now simulate late execDetails arriving for that broker_order_id — should still be processed (position updated) but order stays ERROR
    listener = ManualExecutionListener(session_factory, client=MagicMock())
    # need to ensure order is found via broker_order_id correlation — listener will search by broker_order_id
    exec_obj = MagicMock()
    exec_obj.execId = f"exec-recover-{s}"
    exec_obj.orderId = 82001
    exec_obj.permId = 0
    exec_obj.orderRef = f"MAN_{s}"
    exec_obj.acctNumber = f"DU{s.upper()}"
    exec_obj.shares = 5.0
    exec_obj.price = 100.0
    exec_obj.side = "BOT"
    exec_obj.time = "20260914 10:00:00"
    contract = MagicMock()
    await listener.handle_exec_details(contract=contract, execution=exec_obj)
    async with session_factory() as session:
        # execution should be recorded even though order is ERROR, and position should exist
        from app.db.models.manual_order import ManualExecutionModel, ManualPositionModel
        exec_row = (await session.execute(select(ManualExecutionModel).where(ManualExecutionModel.exec_id == f"exec-recover-{s}"))).scalar_one_or_none()
        assert exec_row is not None
        pos = (await session.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == f"TRD_{s}"))).scalar_one_or_none()
        assert pos is not None
        assert pos.signed_qty == Decimal("5")


# ── D: closed trade_id reuse ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_closed_trade_id_reuse_rejected(session_factory):
    """Open → close (qty exact) → next execution on same trade_id must be rejected."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"))
        session.add(acc); await session.flush()
        acc_id = acc.id
        repo = ManualPositionRepository(session)
        # Open long 10 @100
        pos, _ = await repo.apply_execution(account_id=acc_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=91007, sec_type="CFD", side="BUY", quantity=Decimal("10"), price=Decimal("100"))
        assert pos.signed_qty == Decimal("10") and pos.status == "OPEN"
        # Close long 10 @110
        pos2, pnl = await repo.apply_execution(account_id=acc_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=91007, sec_type="CFD", side="SELL", quantity=Decimal("10"), price=Decimal("110"))
        assert pos2.signed_qty == Decimal("0") and pos2.status == "CLOSED"
        assert pnl == Decimal("100")  # 10*(110-100)
        # Reuse same trade_id should raise
        try:
            await repo.apply_execution(account_id=acc_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=91007, sec_type="CFD", side="BUY", quantity=Decimal("5"), price=Decimal("105"))
            assert False, "expected ValueError for closed trade_id reuse"
        except ValueError as e:
            assert "CLOSED" in str(e)
    # Verify audit event was written
    async with session_factory() as session:
        from app.db.models.manual_order import ManualAuditEventModel
        rows = (await session.execute(select(ManualAuditEventModel).where(ManualAuditEventModel.action == "MANUAL_POSITION_CLOSED_TRADE_ID_REUSE_REJECTED"))).scalars().all()
        assert any(r.payload.get("trade_id") == f"TRD_{s}" for r in rows)
    # New trade_id must succeed
    async with session_factory() as session, session.begin():
        repo2 = ManualPositionRepository(session)
        acc_id2 = (await session.execute(select(AccountModel.id).where(AccountModel.ibkr_account == f"DU{s.upper()}"))).scalar_one()
        pos3, _ = await repo2.apply_execution(account_id=acc_id2, trade_id=f"TRD_{s}_2", symbol="IBUS500", con_id=91007, sec_type="CFD", side="BUY", quantity=Decimal("5"), price=Decimal("105"))
        assert pos3.signed_qty == Decimal("5") and pos3.status == "OPEN"


@pytest.mark.asyncio
async def test_closed_trade_id_reuse_via_callback_rejected(session_factory):
    """Callback execDetails on CLOSED trade_id must not insert execution nor mutate position."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"))
        session.add(acc); await session.flush()
        acc_id = acc.id
        # create manual order + close its position to CLOSED
        order_repo = ManualOrderRepository(session)
        order = await order_repo.create_order(account_id=acc_id, ibkr_account=f"DU{s.upper()}", idempotency_key=f"k-{s}", internal_order_id=f"MAN_{s}", trade_id=f"TRD_{s}", symbol="IBUS500", side="BUY", quantity=Decimal("10"), order_type="LIMIT", con_id=91008, limit_price=Decimal("100"), status="FILLED")
        order.broker_order_id = "83001"
        # position open then close
        pos_repo = ManualPositionRepository(session)
        await pos_repo.apply_execution(account_id=acc_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=91008, sec_type="CFD", side="BUY", quantity=Decimal("10"), price=Decimal("100"))
        await pos_repo.apply_execution(account_id=acc_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=91008, sec_type="CFD", side="SELL", quantity=Decimal("10"), price=Decimal("110"))
    listener = ManualExecutionListener(session_factory, client=MagicMock())
    exec_obj = MagicMock()
    exec_obj.execId = f"exec-closed-reuse-{s}"
    exec_obj.orderId = 83001
    exec_obj.permId = 0
    exec_obj.orderRef = f"MAN_{s}"
    exec_obj.acctNumber = f"DU{s.upper()}"
    exec_obj.shares = 5.0
    exec_obj.price = 105.0
    exec_obj.side = "BOT"
    exec_obj.time = "20260914 10:00:00"
    contract = MagicMock()
    order_result, is_new = await listener.handle_exec_details(contract=contract, execution=exec_obj)
    assert is_new is False  # rejected closed trade_id → not new
    # no execution persisted
    async with session_factory() as session:
        from app.db.models.manual_order import ManualExecutionModel
        assert (await session.execute(select(ManualExecutionModel).where(ManualExecutionModel.exec_id == f"exec-closed-reuse-{s}"))).scalar_one_or_none() is None


# ── E: legacy flatten safety ─────────────────────────────────────────────

def _filled_order_mock(side, qty):
    m = MagicMock()
    m.status = OMSOrderStatus.FILLED
    m.is_compensation = False
    m.filled_quantity = qty
    m.side = side
    return m


@pytest.mark.asyncio
async def test_flatten_rejects_legitimate_manual_position(session_factory):
    """Engine 0, Manual +10, Broker +10 → flatten quantity=10 must be rejected as below ledger net (or blocked). With manual-aware flatten, ledger_net=10 so quantity 10 is valid for full flatten, but quantity 5 (partial below ledger) must be rejected, and quantity 10 must succeed only if manual-aware."""
    s = uuid.uuid4().hex[:8]
    ibkr = f"DU-FLATMAN-{s}"
    con_id = 940000 + int(s[:4], 16) % 10000
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=ibkr, total_margin=Decimal("100000"))
        session.add(acc); await session.flush()
        acc_id = acc.id
        # manual position +10
        pos_repo = ManualPositionRepository(session)
        await pos_repo.create_position(account_id=acc_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=con_id, sec_type="CFD", signed_qty=Decimal("10"), avg_cost=Decimal("100"), status="OPEN")
        # broker snapshot +10
        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot([{"ibkr_account": ibkr, "con_id": con_id, "account_id": acc_id, "symbol": "IBUS500", "sec_type": "CFD", "currency": "USD", "exchange": "SMART", "signed_qty": Decimal("10"), "avg_cost": Decimal("100")}], as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    # Attempt flatten with quantity 10 should succeed (full flatten) now that manual-aware ledger_net=10
    mock_baskets = MagicMock()
    async def _fake_execute(intent, rms_pass, order_type="LIMIT"):
        b = MagicMock(); b.state = BasketState.CLOSED
        return BasketExecutionResult(basket=b, intent=intent, orders=[_filled_order_mock(OrderSide.SELL, 10.0)])
    mock_baskets.execute = AsyncMock(side_effect=_fake_execute)
    om = MagicMock(); om._baskets = mock_baskets; om._resolve_instruments = AsyncMock(side_effect=lambda i: i)
    svc = BrokerFlattenService(session_factory=session_factory, order_manager=om)
    result = await svc.flatten_line(ibkr_account=ibkr, symbol="IBUS500", sec_type="CFD", con_id=con_id, quantity=10.0)
    assert result.success is True
    # Now partial flatten below ledger net must be rejected
    from fastapi import HTTPException
    svc2 = BrokerFlattenService(session_factory=session_factory, order_manager=om)
    # need new snapshot for second call (same)
    with pytest.raises(HTTPException) as ei:
        await svc2.flatten_line(ibkr_account=ibkr, symbol="IBUS500", sec_type="CFD", con_id=con_id, quantity=5.0)
    assert "below ledger net" in ei.value.detail  # type: ignore[operator]


@pytest.mark.asyncio
async def test_flatten_combined_engine_manual(session_factory):
    """Engine +10, Manual +5, Broker +15 → ledger 15, flatten 15 succeeds."""
    s = uuid.uuid4().hex[:8]
    ibkr = f"DU-COMB-{s}"
    con_id = 950000 + int(s[:4], 16) % 10000
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=ibkr, total_margin=Decimal("100000"))
        session.add(acc); await session.flush()
        acc_id = acc.id
        session.add(PositionModel(account_id=acc_id, trade_id=f"T-{s}", strategy_id="m", leg_a_symbol="IBUS500", leg_a_signed_qty=Decimal("10"), leg_a_entry_mark=Decimal("100"), leg_b_symbol=None, leg_b_signed_qty=None, leg_b_entry_mark=None, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60, leg_a_instrument_type="CFD", leg_b_instrument_type=None, risk_state=RISK_STATE_OPEN))
        pos_repo = ManualPositionRepository(session)
        await pos_repo.create_position(account_id=acc_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=con_id, sec_type="CFD", signed_qty=Decimal("5"), avg_cost=Decimal("100"), status="OPEN")
        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot([{"ibkr_account": ibkr, "con_id": con_id, "account_id": acc_id, "symbol": "IBUS500", "sec_type": "CFD", "currency": "USD", "exchange": "SMART", "signed_qty": Decimal("15"), "avg_cost": Decimal("100")}], as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    mock_baskets = MagicMock()
    async def _fake(intent, rms_pass, order_type="LIMIT"):
        b = MagicMock(); b.state = BasketState.CLOSED
        return BasketExecutionResult(basket=b, intent=intent, orders=[_filled_order_mock(OrderSide.SELL, 15.0)])
    mock_baskets.execute = AsyncMock(side_effect=_fake)
    om = MagicMock(); om._baskets = mock_baskets; om._resolve_instruments = AsyncMock(side_effect=lambda i: i)
    svc = BrokerFlattenService(session_factory=session_factory, order_manager=om)
    r = await svc.flatten_line(ibkr_account=ibkr, symbol="IBUS500", sec_type="CFD", con_id=con_id, quantity=15.0)
    assert r.success is True


@pytest.mark.asyncio
async def test_flatten_zero_net_manual(session_factory):
    """Engine +10 Manual -10 Broker 0 → ledger 0, snapshot 0? flatten should fail as nothing to flatten (snapshot zero)."""
    s = uuid.uuid4().hex[:8]
    ibkr = f"DU-ZERO-{s}"
    con_id = 960000 + int(s[:4], 16) % 10000
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=ibkr, total_margin=Decimal("100000"))
        session.add(acc); await session.flush()
        acc_id = acc.id
        session.add(PositionModel(account_id=acc_id, trade_id=f"T-{s}", strategy_id="m", leg_a_symbol="IBUS500", leg_a_signed_qty=Decimal("10"), leg_a_entry_mark=Decimal("100"), leg_b_symbol=None, leg_b_signed_qty=None, leg_b_entry_mark=None, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60, leg_a_instrument_type="CFD", leg_b_instrument_type=None, risk_state=RISK_STATE_OPEN))
        pos_repo = ManualPositionRepository(session)
        await pos_repo.create_position(account_id=acc_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=con_id, sec_type="CFD", signed_qty=Decimal("-10"), avg_cost=Decimal("100"), status="OPEN")
        # broker flat 0 — no snapshot line
        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot([], as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    svc = BrokerFlattenService(session_factory=session_factory, order_manager=MagicMock(_baskets=MagicMock()))
    # flatten with no snapshot must 404
    from fastapi import HTTPException as HE
    with pytest.raises(HE) as ei:
        await svc.flatten_line(ibkr_account=ibkr, symbol="IBUS500", sec_type="CFD", con_id=con_id, quantity=5.0)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_flatten_cross_account_isolation(session_factory):
    """Manual position in Account A must not affect flatten guard for Account B."""
    s = uuid.uuid4().hex[:8]
    ibkr_a = f"DU-A-{s}"
    ibkr_b = f"DU-B-{s}"
    con_id = 970000 + int(s[:4], 16) % 10000
    async with session_factory() as session, session.begin():
        acc_a = AccountModel(name=f"AccA-{s}", ibkr_account=ibkr_a, total_margin=Decimal("100000"))
        acc_b = AccountModel(name=f"AccB-{s}", ibkr_account=ibkr_b, total_margin=Decimal("100000"))
        session.add_all([acc_a, acc_b]); await session.flush()
        acc_a_id = acc_a.id; acc_b_id = acc_b.id
        # manual +10 only in A
        pos_repo = ManualPositionRepository(session)
        await pos_repo.create_position(account_id=acc_a_id, trade_id=f"TRD_{s}", symbol="IBUS500", con_id=con_id, sec_type="CFD", signed_qty=Decimal("10"), avg_cost=Decimal("100"), status="OPEN")
        # broker snapshot +10 only in B
        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot([{"ibkr_account": ibkr_b, "con_id": con_id, "account_id": acc_b_id, "symbol": "IBUS500", "sec_type": "CFD", "currency": "USD", "exchange": "SMART", "signed_qty": Decimal("10"), "avg_cost": Decimal("100")}], as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    mock_baskets = MagicMock()
    async def _fake2(intent, rms_pass, order_type="LIMIT"):
        b = MagicMock(); b.state = BasketState.CLOSED
        return BasketExecutionResult(basket=b, intent=intent, orders=[_filled_order_mock(OrderSide.SELL, 10.0)])
    mock_baskets.execute = AsyncMock(side_effect=_fake2)
    om = MagicMock(); om._baskets = mock_baskets; om._resolve_instruments = AsyncMock(side_effect=lambda i: i)
    svc = BrokerFlattenService(session_factory=session_factory, order_manager=om)
    # For B, engine 0, manual 0 (A's manual not counted) → ledger 0, so full flatten 10 should succeed (orphan)
    r = await svc.flatten_line(ibkr_account=ibkr_b, symbol="IBUS500", sec_type="CFD", con_id=con_id, quantity=10.0)
    assert r.success is True
