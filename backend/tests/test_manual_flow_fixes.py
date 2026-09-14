"""Tests for AAPL forensic fixes: EtradeOnly, error->REJECTED, margin SKIPPED."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_db_session
from app.core.security import create_access_token, get_password_hash
from app.db.models.account import AccountModel
from app.db.models.user import UserModel
from app.db.repositories.manual_repository import ManualOrderRepository
from app.main import app
from app.services.manual_callbacks import ManualExecutionListener


class MockTWSForFix:
    def __init__(self):
        from app.broker.ibkr.gateway_rate_limiter import GatewayRateLimiter
        self._connected = True
        self.managed_accounts = frozenset(["DUR919062"])
        self.next_order_id = 9000
        self._rate_limiter = GatewayRateLimiter()
        self.placed = []

    def is_connected(self): return True
    def allocate_next_order_id(self):
        cur = self.next_order_id; self.next_order_id += 1; return cur
    def placeOrder(self, oid, contract, order):
        self.placed.append((oid, contract, order))
    def cancelOrder(self, oid): pass


class MockAdapterWhatIf:
    def __init__(self, unknown=False):
        from app.oms.ibkr_adapter import WhatIfResult
        self.unknown = unknown
        self.result = WhatIfResult(order_id=1, unknown=unknown, init_margin_change=Decimal("10"), maint_margin_change=Decimal("5"))
    async def probe_margin(self, **kw):
        return self.result


@pytest.mark.asyncio
async def test_manual_order_sets_etrade_only_false(session_factory):
    """Fix 10268: Manual MARKET order must set eTradeOnly=False and firmQuoteOnly=False."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:4]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc); await session.commit(); await session.refresh(acc)
        user = UserModel(email=f"u_{s}@ex.com", password_hash=get_password_hash("p"), role="user", is_active=True, ibkr_account_id=acc.id)
        session.add(user); await session.commit(); await session.refresh(user)
        token = create_access_token({"sub": str(user.id), "role": "user", "email": user.email})
        acc_code = acc.ibkr_account
    mock_client = MockTWSForFix()
    mock_client.managed_accounts = frozenset([acc_code])
    mock_adapter = MockAdapterWhatIf(unknown=False)
    app.state.client = mock_client
    app.state.ibkr_adapter = mock_adapter
    async def _ov():
        async with session_factory() as sess:
            yield sess
    from app.api.deps import get_db_session as G
    app.dependency_overrides[G] = _ov
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/api/v1/manual/orders?ibkr_account={acc_code}", json={
                "idempotency_key": f"k-{s}", "symbol": "AAPL", "con_id": 120549942, "sec_type": "CFD", "exchange": "SMART", "currency": "USD",
                "side": "BUY", "quantity": "10", "order_type": "MARKET"}, headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200, resp.text
        assert len(mock_client.placed) == 1
        _, _, order = mock_client.placed[0]
        assert order.eTradeOnly is False
        assert order.firmQuoteOnly is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_error_callback_moves_submitted_to_rejected(session_factory):
    """10268 error should move SUBMITTED → REJECTED, not stuck WORKING."""
    s = uuid.uuid4().hex[:6]
    broker_id = str(99000 + int(s[:4], 16) % 1000)
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc); await session.commit(); await session.refresh(acc)
        acc_id = acc.id; acc_code = acc.ibkr_account
        repo = ManualOrderRepository(session)
        order = await repo.create_order(account_id=acc_id, ibkr_account=acc_code, idempotency_key=f"k2-{s}", internal_order_id=f"MAN_{s}", trade_id=f"TRD_{s}", symbol="AAPL", side="BUY", quantity=Decimal("10"), order_type="MARKET", con_id=120549942, sec_type="CFD", status="SUBMITTED")
        order.broker_order_id = broker_id
        await session.commit()
    listener = ManualExecutionListener(session_factory, client=MagicMock())
    await listener.handle_error(reqId=int(broker_id), errorCode=10268, errorString="The 'EtradeOnly' order attribute is not supported.")
    async with session_factory() as session:
        from app.db.models.manual_order import ManualOrderModel
        r = await session.execute(select(ManualOrderModel).where(ManualOrderModel.broker_order_id==broker_id))
        o = r.scalar_one()
        assert o.status == "REJECTED"
        assert "10268" in (o.reject_reason or "")


@pytest.mark.asyncio
async def test_preview_margin_skipped_when_disabled(session_factory, monkeypatch):
    """When MARGIN_WHATIF_ENABLED=false, preview should be SKIPPED not UNAVAILABLE."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:4]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc); await session.commit(); await session.refresh(acc)
        user = UserModel(email=f"u_{s}@ex.com", password_hash=get_password_hash("p"), role="user", is_active=True, ibkr_account_id=acc.id)
        session.add(user); await session.commit(); await session.refresh(user)
        token = create_access_token({"sub": str(user.id), "role": "user", "email": user.email})
        acc_code = acc.ibkr_account
    # Mock adapter that would return unknown if called, but we want SKIPPED path
    mock_client = MockTWSForFix()
    mock_client.managed_accounts = frozenset([acc_code])
    app.state.client = mock_client
    app.state.ibkr_adapter = MockAdapterWhatIf(unknown=True)
    from unittest.mock import patch
    mock_settings = MagicMock()
    mock_settings.margin_whatif_enabled = False
    mock_settings.margin_whatif_timeout_sec = 5.0
    from app.services.manual_trading import ManualTradingService
    from app.schemas.manual_schemas import GatewayEnvironmentMode, ManualOrderPreviewRequest
    async with session_factory() as session:
        acc_obj = (await session.execute(select(AccountModel).where(AccountModel.ibkr_account==acc_code))).scalar_one()
        svc = ManualTradingService(session, client=mock_client, ibkr_adapter=MockAdapterWhatIf(unknown=True))
        with patch("app.core.config.get_settings", return_value=mock_settings):
            req = ManualOrderPreviewRequest(symbol="AAPL", con_id=120549942, sec_type="CFD", exchange="SMART", currency="USD", side="BUY", quantity=Decimal("10"), order_type="MARKET")
            resp = await svc.preview_order(acc_obj, req, GatewayEnvironmentMode.UNKNOWN)
            assert resp.margin_status == "SKIPPED"
            assert resp.valid is True
            assert not any("unavailable" in w.lower() for w in resp.warnings)


def test_frontend_no_advanced_diagnostics():
    """Advanced Diagnostics section must not be rendered."""
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[2].parent / "frontend" / "src" / "pages" / "ManualTradePage.tsx"
    if not p.exists():
        p = pathlib.Path("/home/dev3/Documents/ibkr-paper-trading-system/frontend/src/pages/ManualTradePage.tsx")
    text = p.read_text()
    assert "Advanced diagnostics" not in text
    assert "Advanced Diagnostics" not in text
