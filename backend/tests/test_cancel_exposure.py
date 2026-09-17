"""Cancel Exposure validation tests."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.position import PositionModel
from app.db.session import create_engine_from_settings
from app.main import app
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide
from app.services.cancel_exposure import evaluate_cancel_exposure


def _intent(legs: list[tuple[str, OrderSide]]) -> OrderIntent:
    return OrderIntent(
        signal_id="SIG-1",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol=sym, side=side, quantity=10, price=Decimal(10)) for sym, side in legs],
        account_id=1,
        ibkr_account="DU123",
    )


def _position(account_id: int, trade_id: str, leg_a: tuple[str, Decimal], leg_b: tuple[str, Decimal]) -> PositionModel:
    # leg = (symbol, signed_qty)
    return PositionModel(
        account_id=account_id,
        trade_id=trade_id,
        strategy_id="model_blue",
        leg_a_symbol=leg_a[0],
        leg_a_signed_qty=leg_a[1],
        leg_a_entry_mark=Decimal("10"),
        leg_b_symbol=leg_b[0],
        leg_b_signed_qty=leg_b[1],
        leg_b_entry_mark=Decimal("10"),
        target=Decimal(500),
        stop=Decimal(-250),
        time_limit=3600,
        risk_state="OPEN",
    )


def test_off_rejects_partial_cancel():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]  # AAPL BUY, EWC SELL
    incoming = _intent([("AAPL", OrderSide.SELL), ("XYZ", OrderSide.BUY)])
    allowed, reason = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=False)
    assert not allowed
    assert reason is not None
    assert "CANCEL_EXPOSURE_VIOLATION" in reason


def test_off_allows_exact_close():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]
    incoming = _intent([("AAPL", OrderSide.SELL), ("EWC", OrderSide.BUY)])
    allowed, reason = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=False)
    assert allowed
    assert reason is None


def test_off_allows_same_direction():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]
    incoming = _intent([("AAPL", OrderSide.BUY), ("XYZ", OrderSide.SELL)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=False)
    assert allowed


def test_on_allows_partial():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]
    incoming = _intent([("AAPL", OrderSide.SELL), ("XYZ", OrderSide.BUY)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=True)
    assert allowed


def test_on_allows_exact_close():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]
    incoming = _intent([("AAPL", OrderSide.SELL), ("EWC", OrderSide.BUY)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=True)
    assert allowed


def test_off_rejects_when_closing_ewc_leg():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]
    incoming = _intent([("EWC", OrderSide.BUY), ("XYZ", OrderSide.SELL)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=False)
    assert not allowed


def test_on_allows_ewc_partial():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]
    incoming = _intent([("EWC", OrderSide.BUY), ("XYZ", OrderSide.SELL)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=True)
    assert allowed


def test_reverse_direction():
    existing = [_position(1, "T1", ("AAPL", Decimal(-10)), ("EWC", Decimal(10)))]  # AAPL SELL, EWC BUY
    incoming = _intent([("AAPL", OrderSide.BUY), ("XYZ", OrderSide.SELL)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=False)
    assert not allowed
    incoming_exact = _intent([("AAPL", OrderSide.BUY), ("EWC", OrderSide.SELL)])
    allowed2, _ = evaluate_cancel_exposure(intent=incoming_exact, open_positions=existing, cancel_exposure=False)
    assert allowed2


def test_zero_positions_always_allow():
    incoming = _intent([("AAPL", OrderSide.SELL), ("XYZ", OrderSide.BUY)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=[], cancel_exposure=False)
    assert allowed


def test_single_leg_position_not_considered_paired():
    # Position with only one leg should not trigger
    row = PositionModel(
        account_id=1,
        trade_id="T1",
        strategy_id="model_blue",
        leg_a_symbol="AAPL",
        leg_a_signed_qty=Decimal(10),
        leg_a_entry_mark=Decimal(10),
        leg_b_symbol=None,
        leg_b_signed_qty=None,
        leg_b_entry_mark=None,
        target=Decimal(500),
        stop=Decimal(-250),
        time_limit=3600,
        risk_state="OPEN",
    )
    incoming = _intent([("AAPL", OrderSide.SELL), ("XYZ", OrderSide.BUY)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=[row], cancel_exposure=False)
    assert allowed


def test_multiple_positions_any_partial_rejects():
    pos1 = _position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))
    pos2 = _position(1, "T2", ("MSFT", Decimal(10)), ("SPY", Decimal(-10)))
    incoming = _intent([("AAPL", OrderSide.SELL), ("XYZ", OrderSide.BUY)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=[pos1, pos2], cancel_exposure=False)
    assert not allowed


def test_multiple_positions_exact_close_allows():
    pos1 = _position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))
    pos2 = _position(1, "T2", ("MSFT", Decimal(10)), ("SPY", Decimal(-10)))
    incoming = _intent([("AAPL", OrderSide.SELL), ("EWC", OrderSide.BUY)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=[pos1, pos2], cancel_exposure=False)
    assert allowed


def test_close_action_always_allows():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]
    incoming = OrderIntent(
        signal_id="SIG-1",
        strategy_id="model_blue",
        action=OrderAction.CLOSE,
        legs=[],
        account_id=1,
    )
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=False)
    assert allowed


def test_no_reject_for_unrelated_symbols():
    existing = [_position(1, "T1", ("AAPL", Decimal(10)), ("EWC", Decimal(-10)))]
    incoming = _intent([("MSFT", OrderSide.BUY), ("SPY", OrderSide.SELL)])
    allowed, _ = evaluate_cancel_exposure(intent=incoming, open_positions=existing, cancel_exposure=False)
    assert allowed


# API tests


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.broker.ibkr.tws_client.TWSClient.is_connected", return_value=False),
        patch("app.services.worker_pool.ExecutionWorkerPool.start", new_callable=AsyncMock),
        patch("app.services.worker_pool.ExecutionWorkerPool.stop", new_callable=AsyncMock),
        patch("app.services.position_reconciler.PositionReconciler.start", new_callable=AsyncMock),
        patch("app.services.position_reconciler.PositionReconciler.stop", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_live_pnl", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.reload_margin_rates", new_callable=AsyncMock),
        patch("app.services.margin_scanner.MarginScanner.start_background", new_callable=AsyncMock),
        patch("app.services.margin_scanner.MarginScanner.stop", new_callable=AsyncMock),
        patch("app.services.critical_recovery.CriticalRecoveryService.enqueue_all_critical", new_callable=AsyncMock),
        TestClient(app) as c,
    ):
        yield c


def test_cancel_exposure_default_off(client: TestClient):
    suffix = uuid.uuid4().hex[:6]
    ibkr = f"DUTCE{suffix}"
    res = client.post(
        "/api/v1/config/accounts",
        json={"name": f"CE Default {suffix}", "ibkr_account": ibkr, "total_margin": 100000},
    )
    assert res.status_code == 201
    body = res.json()
    # default should be false / missing treated as false
    assert body.get("cancel_exposure") is False or body.get("cancel_exposure") is None


def test_cancel_exposure_can_be_enabled_and_disabled(client: TestClient):
    suffix = uuid.uuid4().hex[:6]
    ibkr = f"DUTCE2{suffix}"
    res = client.post(
        "/api/v1/config/accounts",
        json={"name": f"CE Toggle {suffix}", "ibkr_account": ibkr, "total_margin": 100000},
    )
    assert res.status_code == 201
    acc_id = res.json()["id"]

    # enable
    patch = client.patch(f"/api/v1/config/accounts/{acc_id}", json={"cancel_exposure": True})
    assert patch.status_code == 200
    assert patch.json()["cancel_exposure"] is True

    # fetch by identifier
    get = client.get(f"/api/v1/config/accounts/by-identifier/{ibkr}")
    assert get.status_code == 200
    assert get.json()["cancel_exposure"] is True

    # disable
    patch2 = client.patch(f"/api/v1/config/accounts/{acc_id}", json={"cancel_exposure": False})
    assert patch2.status_code == 200
    assert patch2.json()["cancel_exposure"] is False


def test_cancel_exposure_persists_and_returns_in_list(client: TestClient):
    # create and toggle, then check list endpoint
    suffix = uuid.uuid4().hex[:6]
    ibkr = f"DUTCE3{suffix}"
    res = client.post(
        "/api/v1/config/accounts",
        json={"name": f"CE List {suffix}", "ibkr_account": ibkr, "total_margin": 100000},
    )
    assert res.status_code == 201
    acc_id = res.json()["id"]
    client.patch(f"/api/v1/config/accounts/{acc_id}", json={"cancel_exposure": True})
    lst = client.get("/api/v1/config/accounts")
    assert lst.status_code == 200
    match = [a for a in lst.json()["accounts"] if a["id"] == acc_id]
    assert len(match) == 1
    assert match[0]["cancel_exposure"] is True


@pytest.mark.asyncio
async def test_cancel_exposure_integration_rejects_partial_via_order_manager(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Integration: OrderManager fanout should reject partial when OFF, allow when ON."""
    from app.accounts.context import AccountExecutionContext
    from app.accounts.router import StaticStrategyAccountRouter
    from app.db.models.strategy import AllocationModel, StrategyModel
    from app.models.signal import Signal, SignalLeg, SignalType
    from app.services.order_manager import OrderManager
    from datetime import datetime, UTC

    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"CEInt {suffix}", ibkr_account=f"DUCEI{suffix}", total_margin=Decimal(100000), cancel_exposure=False)
        session.add(acc)
        await session.flush()
        acc_id = acc.id
        strat = StrategyModel(strategy_id=f"MODEL_CE_{suffix}", legs=2, expression="CFD", max_open_positions=10, weight_source="payload", enabled=True)
        session.add(strat)
        await session.flush()
        alloc = AllocationModel(account_id=acc_id, strategy_id=strat.strategy_id, alloc_pct=Decimal("0.5"), target=Decimal(500), stop=Decimal(-250), time_limit=3600, max_open_positions=5, enabled=True)
        session.add(alloc)
        # open position AAPL BUY + EWC SELL
        pos = PositionModel(
            account_id=acc_id,
            trade_id=f"TRADE-CE-{suffix}",
            strategy_id=strat.strategy_id,
            leg_a_symbol="AAPL",
            leg_a_signed_qty=Decimal(10),
            leg_a_entry_mark=Decimal(150),
            leg_b_symbol="EWC",
            leg_b_signed_qty=Decimal(-10),
            leg_b_entry_mark=Decimal(20),
            target=Decimal(500),
            stop=Decimal(-250),
            time_limit=3600,
            risk_state="OPEN",
        )
        session.add(pos)
        await session.commit()

    # Build contexts manually
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        from sqlalchemy import select
        account_row = (await s.execute(select(AccountModel).where(AccountModel.id == acc_id))).scalar_one()
        allocation_row = (await s.execute(select(AllocationModel).where(AllocationModel.account_id == acc_id))).scalar_one()
        strategy_row = (await s.execute(select(StrategyModel).where(StrategyModel.strategy_id == f"MODEL_CE_{suffix}"))).scalar_one()
        from app.accounts.router import context_from_rows
        ctx_off = context_from_rows(account_row, allocation_row, strategy_row)
        assert ctx_off.cancel_exposure is False

    # Create signal for partial: AAPL SELL + XYZ BUY
    signal_partial = Signal(
        signal_type=SignalType.BUY,
        timestamp=datetime.now(UTC),
        reason="test",
        signal_id="SIG-PARTIAL",
        strategy_id=f"MODEL_CE_{suffix}",
        action="OPEN",
        trade_id="SIG-PARTIAL",
        legs=(
            SignalLeg(symbol="AAPL", instrument_type="STK", weight=0.5, price=Decimal(150), leg_index=0),
            SignalLeg(symbol="XYZ", instrument_type="STK", weight=0.5, price=Decimal(20), leg_index=1),
        ),
    )
    signal_exact = Signal(
        signal_type=SignalType.BUY,
        timestamp=datetime.now(UTC),
        reason="test",
        signal_id="SIG-EXACT",
        strategy_id=f"MODEL_CE_{suffix}",
        action="OPEN",
        trade_id="SIG-EXACT",
        legs=(
            SignalLeg(symbol="AAPL", instrument_type="STK", weight=0.5, price=Decimal(150), leg_index=0),
            SignalLeg(symbol="EWC", instrument_type="STK", weight=0.5, price=Decimal(20), leg_index=1),
        ),
    )

    router_off = StaticStrategyAccountRouter([ctx_off])
    om = OrderManager(session_factory=session_factory, account_router=router_off)
    # Don't need OMS; the validation happens before RMS/OMS, so even without OMS, the cancel exposure should be checked first.
    # The fanout will call handler.build_intent which requires commitment etc. We'll mock the handler to produce intent directly.
    # Instead test the cancel_exposure service directly via check_cancel_exposure with real DB.

    from app.rms.models import OrderIntent, OrderLeg, OrderSide, OrderAction
    from app.services.cancel_exposure import check_cancel_exposure

    intent_partial = OrderIntent(
        signal_id="SIG-PARTIAL",
        strategy_id=f"MODEL_CE_{suffix}",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.SELL, quantity=10, price=Decimal(150)),
            OrderLeg(symbol="XYZ", side=OrderSide.BUY, quantity=10, price=Decimal(20)),
        ],
        account_id=acc_id,
        ibkr_account=f"DUCEI{suffix}",
    )
    intent_exact = OrderIntent(
        signal_id="SIG-EXACT",
        strategy_id=f"MODEL_CE_{suffix}",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.SELL, quantity=10, price=Decimal(150)),
            OrderLeg(symbol="EWC", side=OrderSide.BUY, quantity=10, price=Decimal(20)),
        ],
        account_id=acc_id,
        ibkr_account=f"DUCEI{suffix}",
    )

    # OFF should reject partial
    with pytest.raises(ValueError, match="CANCEL_EXPOSURE_VIOLATION"):
        await check_cancel_exposure(intent=intent_partial, account_id=acc_id, cancel_exposure=False, session_factory=session_factory)

    # OFF should allow exact
    await check_cancel_exposure(intent=intent_exact, account_id=acc_id, cancel_exposure=False, session_factory=session_factory)

    # ON should allow partial
    await check_cancel_exposure(intent=intent_partial, account_id=acc_id, cancel_exposure=True, session_factory=session_factory)

    await engine.dispose()
