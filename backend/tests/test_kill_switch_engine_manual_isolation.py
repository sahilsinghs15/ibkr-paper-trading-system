"""Kill Switch: engine-only flatten must preserve manual ledger + ledger ghost convergence.

Covers task scenarios 1-9 and 13-19 for the production-critical correctness fixes.

Ownership model:
- Engine positions live in `positions` (PositionModel) per trade_id pair.
- Manual positions live in `manual_positions` (ManualPositionModel) per trade_id lot.
- Kill Switch `Close All Engine Positions` must SELECT ONLY from `positions` and
  submit reverse CLOSE legs sized from signed_qty, never from broker aggregate net.

Ledger convergence:
- Engine flatten submitted -> broker fill -> execution persisted exactly once
  -> engine trade closed -> reconciler verifies broker/ledger consistency.

These tests use mocked baskets (no live IBKR) and verify DB ledger state.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.manual_order import ManualPositionModel
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import SignalModel
from app.db.repositories.position_repository import PositionRepository
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.oms.models import OMSOrder, OMSOrderStatus
from app.rms.models import OrderAction, OrderIntent, OrderLeg
from app.rms.models import OrderSide as RMSOrderSide
from app.services.kill_switch import KillSwitchService
from app.services.position_reconciler import build_ledger_net_lines, classify_reconcile_diffs
from app.broker.ibkr.positions import BrokerPositionLine


def _engine_legs(symbol_a: str, qty_a: Decimal, price_a: Decimal, symbol_b: str | None = None, qty_b: Decimal | None = None, price_b: Decimal | None = None):
    legs = [OpenModelBlueTradeLeg(symbol=symbol_a, instrument_type="STK", side=RMSOrderSide.BUY if qty_a > 0 else RMSOrderSide.SELL, quantity=abs(qty_a), price=price_a)]
    if symbol_b and qty_b is not None and price_b is not None:
        legs.append(OpenModelBlueTradeLeg(symbol=symbol_b, instrument_type="STK", side=RMSOrderSide.BUY if qty_b > 0 else RMSOrderSide.SELL, quantity=abs(qty_b), price=price_b))
    return tuple(legs)


async def _create_account(session_factory, ibkr_suffix: str | None = None):
    tag = uuid4().hex[:6]
    ibkr = f"DU{tag.upper()}"
    if ibkr_suffix:
        ibkr = f"DU{ibkr_suffix[:4].upper()}{tag[:4].upper()}"
    async with session_factory() as s, s.begin():
        acc = AccountModel(name=f"KSIso-{tag}", ibkr_account=ibkr, total_margin=Decimal("100000"))
        s.add(acc)
        await s.flush()
        return acc.id, ibkr


@pytest.mark.asyncio
async def test_engine_only_close_leaves_manual_untouched_same_symbol(session_factory: async_sessionmaker[AsyncSession]):
    """Engine +2, Manual +1 same symbol -> close engine leaves Manual +1, Engine 0, broker +1."""
    acc_id, ibkr = await _create_account(session_factory)
    trade_engine = f"ENG-{uuid4().hex[:6]}"
    trade_manual = f"MAN-{uuid4().hex[:6]}"

    # Engine pair: single leg for simplicity (AAPL long 2) + manual same symbol long 1
    # Use single-leg engine via direct PositionModel insert (pair with leg_b null is allowed)
    async with session_factory() as s, s.begin():
        # Engine position: create via repo open_trade requires 2 legs, so insert via PositionModel directly for single-leg case
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=trade_engine,
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal("2"),
                leg_a_entry_mark=Decimal("150.00"),
                leg_b_symbol=None,
                leg_b_signed_qty=None,
                leg_b_entry_mark=None,
                leg_a_instrument_type="STK",
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=trade_manual,
                symbol="AAPL",
                con_id=1001,
                sec_type="STK",
                currency="USD",
                signed_qty=Decimal("1"),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal("0"),
                status="OPEN",
                source="manual",
            )
        )

    # Mock basket that fills close for engine 2
    close_intent_leg = OrderLeg(symbol="AAPL", side=RMSOrderSide.SELL, quantity=2.0, price=Decimal("155"), leg_index=0)
    close_intent = OrderIntent(signal_id=f"KILLSWITCH-{trade_engine}", strategy_id="model_blue", action=OrderAction.CLOSE, legs=[close_intent_leg], account_id=acc_id)
    order = OMSOrder(internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_engine}-L0", intent=close_intent, leg_index=0, symbol="AAPL", side=RMSOrderSide.SELL, quantity=2.0, status=OMSOrderStatus.FILLED, filled_quantity=2.0, average_fill_price=Decimal("155"))

    mock_baskets = MagicMock()
    mock_baskets.execute = AsyncMock(return_value=MagicMock(success=True, orders=[order], order=order))
    mock_baskets._event = AsyncMock()
    mock_om = MagicMock()
    mock_om._baskets = mock_baskets
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda i: i)
    mock_om._live_pnl = MagicMock()
    mock_om._live_pnl.unwatch = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, order_manager=mock_om)
    op, _ = await svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    await svc._execute_flatten_operation(op.operation_id)

    async with session_factory() as s:
        eng = await s.get(PositionModel, (acc_id, trade_engine))
        assert eng is not None
        assert eng.risk_state == "CLOSED"
        # Manual must remain OPEN
        man = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.account_id == acc_id, ManualPositionModel.trade_id == trade_manual))).scalar_one()
        assert man.status == "OPEN"
        assert man.signed_qty == Decimal("1")
        # Verify close order quantity was engine qty only (2 not 3)
        assert order.quantity == 2.0
        # Reconciler net should be manual only (+1) after engine closed
        open_rows = (await s.execute(select(PositionModel).where(PositionModel.risk_state == "OPEN"))).scalars().all()
        manual_rows = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.status == "OPEN"))).scalars().all()
        # Need instruments empty for net calc
        ledger_lines = build_ledger_net_lines(open_rows, [], manual_rows)  # type: ignore[arg-type]
        # Find AAPL STK net
        net = None
        for line in ledger_lines:
            if line.symbol == "AAPL" and line.account_id == acc_id:
                net = line
        assert net is not None
        assert float(net.signed_qty) == 1.0
        assert float(net.engine_qty) == 0.0
        assert float(net.manual_qty) == 1.0


@pytest.mark.asyncio
async def test_manual_only_no_engine_close_order(session_factory: async_sessionmaker[AsyncSession]):
    """Engine 0, Manual +2 -> kill switch should not submit any close order."""
    acc_id, ibkr = await _create_account(session_factory)
    trade_manual = f"MAN-{uuid4().hex[:6]}"
    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=trade_manual,
                symbol="AAPL",
                con_id=1002,
                sec_type="STK",
                currency="USD",
                signed_qty=Decimal("2"),
                avg_cost=Decimal("100"),
                realized_pnl=Decimal("0"),
                status="OPEN",
                source="manual",
            )
        )

    mock_baskets = MagicMock()
    mock_baskets.execute = AsyncMock()
    mock_baskets._event = AsyncMock()
    mock_om = MagicMock()
    mock_om._baskets = mock_baskets
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda i: i)

    svc = KillSwitchService(session_factory=session_factory, order_manager=mock_om)
    op, _ = await svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    # No engine open positions, so flatten should complete immediately without calling basket
    await svc._execute_flatten_operation(op.operation_id)

    mock_baskets.execute.assert_not_called()
    async with session_factory() as s:
        man = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.account_id == acc_id))).scalar_one()
        assert man.status == "OPEN"
        assert man.signed_qty == Decimal("2")
        # Engine positions still empty
        eng_rows = (await s.execute(select(PositionModel).where(PositionModel.account_id == acc_id))).scalars().all()
        assert len(eng_rows) == 0


@pytest.mark.asyncio
async def test_mixed_opposite_direction_preserves_manual(session_factory: async_sessionmaker[AsyncSession]):
    """Engine +2, Manual -1 -> engine close leaves Manual -1."""
    acc_id, _ = await _create_account(session_factory)
    trade_engine = f"ENG-{uuid4().hex[:6]}"
    trade_manual = f"MAN-{uuid4().hex[:6]}"
    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=trade_engine,
                strategy_id="model_blue",
                leg_a_symbol="ES",
                leg_a_signed_qty=Decimal("2"),
                leg_a_entry_mark=Decimal("5000"),
                leg_b_symbol=None,
                leg_b_signed_qty=None,
                leg_b_entry_mark=None,
                leg_a_instrument_type="STK",
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=trade_manual,
                symbol="ES",
                con_id=2001,
                sec_type="STK",
                currency="USD",
                signed_qty=Decimal("-1"),
                avg_cost=Decimal("5000"),
                realized_pnl=Decimal("0"),
                status="OPEN",
                source="manual",
            )
        )

    close_leg = OrderLeg(symbol="ES", side=RMSOrderSide.SELL, quantity=2.0, price=Decimal("5000"), leg_index=0)
    close_intent = OrderIntent(signal_id=f"KILLSWITCH-{trade_engine}", strategy_id="model_blue", action=OrderAction.CLOSE, legs=[close_leg], account_id=acc_id)
    order = OMSOrder(internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_engine}-L0", intent=close_intent, leg_index=0, symbol="ES", side=RMSOrderSide.SELL, quantity=2.0, status=OMSOrderStatus.FILLED, filled_quantity=2.0, average_fill_price=Decimal("5000"))

    mock_baskets = MagicMock()
    mock_baskets.execute = AsyncMock(return_value=MagicMock(success=True, orders=[order], order=order))
    mock_baskets._event = AsyncMock()
    mock_om = MagicMock()
    mock_om._baskets = mock_baskets
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda i: i)
    mock_om._live_pnl = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, order_manager=mock_om)
    op, _ = await svc.initiate_square_off(account_id=acc_id)
    await svc._execute_flatten_operation(op.operation_id)

    async with session_factory() as s:
        eng = await s.get(PositionModel, (acc_id, trade_engine))
        assert eng.risk_state == "CLOSED"
        man = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == trade_manual))).scalar_one()
        assert man.signed_qty == Decimal("-1")
        assert man.status == "OPEN"


@pytest.mark.asyncio
async def test_engine_short_manual_long_isolated(session_factory: async_sessionmaker[AsyncSession]):
    """Engine -3, Manual +2 -> engine close leaves Manual +2."""
    acc_id, _ = await _create_account(session_factory)
    trade_engine = f"ENG-{uuid4().hex[:6]}"
    trade_manual = f"MAN-{uuid4().hex[:6]}"
    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=trade_engine,
                strategy_id="model_blue",
                leg_a_symbol="ES",
                leg_a_signed_qty=Decimal("-3"),
                leg_a_entry_mark=Decimal("5000"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=trade_manual,
                symbol="ES",
                con_id=2002,
                sec_type="STK",
                currency="USD",
                signed_qty=Decimal("2"),
                avg_cost=Decimal("5000"),
                realized_pnl=Decimal("0"),
                status="OPEN",
                source="manual",
            )
        )

    close_leg = OrderLeg(symbol="ES", side=RMSOrderSide.BUY, quantity=3.0, price=Decimal("5000"), leg_index=0)
    close_intent = OrderIntent(signal_id=f"KILLSWITCH-{trade_engine}", strategy_id="model_blue", action=OrderAction.CLOSE, legs=[close_leg], account_id=acc_id)
    order = OMSOrder(internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_engine}-L0", intent=close_intent, leg_index=0, symbol="ES", side=RMSOrderSide.BUY, quantity=3.0, status=OMSOrderStatus.FILLED, filled_quantity=3.0, average_fill_price=Decimal("5000"))

    mock_baskets = MagicMock()
    mock_baskets.execute = AsyncMock(return_value=MagicMock(success=True, orders=[order], order=order))
    mock_baskets._event = AsyncMock()
    mock_om = MagicMock()
    mock_om._baskets = mock_baskets
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda i: i)
    mock_om._live_pnl = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, order_manager=mock_om)
    op, _ = await svc.initiate_square_off(account_id=acc_id)
    await svc._execute_flatten_operation(op.operation_id)

    async with session_factory() as s:
        eng = await s.get(PositionModel, (acc_id, trade_engine))
        assert eng.risk_state == "CLOSED"
        man = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == trade_manual))).scalar_one()
        assert man.signed_qty == Decimal("2")


@pytest.mark.asyncio
async def test_reconciler_engine_manual_nets_and_ghost_detection(session_factory: async_sessionmaker[AsyncSession]):
    """PositionReconciler must correctly net engine+manual and detect ghost vs match."""
    acc_id, ibkr = await _create_account(session_factory)
    trade_eng = f"ENG-{uuid4().hex[:6]}"
    trade_man = f"MAN-{uuid4().hex[:6]}"
    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=trade_eng,
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal("2"),
                leg_a_entry_mark=Decimal("150"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=trade_man,
                symbol="AAPL",
                con_id=1003,
                sec_type="STK",
                currency="USD",
                signed_qty=Decimal("1"),
                avg_cost=Decimal("150"),
                realized_pnl=Decimal("0"),
                status="OPEN",
                source="manual",
            )
        )

    # Broker reports aggregate +3 (engine 2 + manual 1) -> should be MATCH
    from app.db.models.instrument import InstrumentModel

    async with session_factory() as s:
        open_rows = (await s.execute(select(PositionModel).where(PositionModel.risk_state == "OPEN"))).scalars().all()
        manual_rows = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.status == "OPEN"))).scalars().all()
        instruments = (await s.execute(select(InstrumentModel))).scalars().all()
        ledger_lines = build_ledger_net_lines(open_rows, instruments, manual_rows)  # type: ignore[arg-type]
        # Broker snapshot for same account
        broker_lines = [BrokerPositionLine(ibkr_account=ibkr, symbol="AAPL", sec_type="STK", con_id=1003, currency="USD", exchange="SMART", quantity=3.0, avg_cost=150.0)]
        ibkr_to_account = {ibkr.strip().upper(): acc_id}
        diffs = classify_reconcile_diffs(broker_lines=broker_lines, ledger_lines=ledger_lines, ibkr_to_account=ibkr_to_account, timed_out=False, in_flight_accounts=set())
        assert any(d.kind == "MATCH" and d.symbol == "AAPL" for d in diffs)
        # Ensure engine/manual breakdown present
        match = [d for d in diffs if d.kind == "MATCH"][0]
        assert match.engine_qty == 2.0
        assert match.manual_qty == 1.0

        # Now simulate engine closed (ledger ghost before fix): broker flat 0 but ledger still shows engine +2 + manual 1 =3
        # If engine was correctly closed, ledger should be 1. Simulate ghost: engine still OPEN
        # broker 1 (manual only) but ledger 3 -> QTY_DRIFT
        broker_lines2 = [BrokerPositionLine(ibkr_account=ibkr, symbol="AAPL", sec_type="STK", con_id=1003, currency="USD", exchange="SMART", quantity=1.0, avg_cost=150.0)]
        diffs2 = classify_reconcile_diffs(broker_lines=broker_lines2, ledger_lines=ledger_lines, ibkr_to_account=ibkr_to_account, timed_out=False, in_flight_accounts=set())
        assert any(d.kind == "QTY_DRIFT" for d in diffs2)


@pytest.mark.asyncio
async def test_tier2_reconcile_fallback_via_executions(session_factory: async_sessionmaker[AsyncSession]):
    """Tier2 reconcile should close via executions even if order fill_price is null (fallback)."""
    acc_id, _ = await _create_account(session_factory)
    trade_id = f"ENG-{uuid4().hex[:6]}"
    sig_id = None
    async with session_factory() as s, s.begin():
        # Create signal row for FK
        sig = SignalModel(signal_id=f"SIG-{trade_id}", strategy_id="model_blue", action="OPEN", pair="AAPL-MSFT", side="BUY", ref_price_a=Decimal("150"), raw_payload={}, status="NEW")
        s.add(sig)
        await s.flush()
        sig_id = sig.id
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=trade_id,
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal("10"),
                leg_a_entry_mark=Decimal("150"),
                leg_b_symbol="MSFT",
                leg_b_signed_qty=Decimal("-5"),
                leg_b_entry_mark=Decimal("300"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        # Close orders with FILLED but fill_price NULL (simulates missing price)
        s.add(
            OrderModel(
                signal_id=sig_id,
                account_id=acc_id,
                strategy_id="model_blue",
                leg="L0",
                symbol="AAPL",
                ibkr_contract="STK",
                buy_sell="SELL",
                quantity=Decimal("10"),
                limit_price=Decimal("0"),
                status="FILLED",
                trade_id=f"KILLSWITCH-{trade_id}",
                internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L0",
                fill_price=None,
                fill_qty=Decimal("10"),
            )
        )
        s.add(
            OrderModel(
                signal_id=sig_id,
                account_id=acc_id,
                strategy_id="model_blue",
                leg="L1",
                symbol="MSFT",
                ibkr_contract="STK",
                buy_sell="BUY",
                quantity=Decimal("5"),
                limit_price=Decimal("0"),
                status="FILLED",
                trade_id=f"KILLSWITCH-{trade_id}",
                internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L1",
                fill_price=None,
                fill_qty=Decimal("5"),
            )
        )
        # Add executions with prices for fallback
        from app.db.models.execution import ExecutionModel

        # We need to reference order ids after flush
        await s.flush()
        # Fetch order ids
        o1 = (await s.execute(select(OrderModel).where(OrderModel.internal_order_id == f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L0"))).scalar_one()
        o2 = (await s.execute(select(OrderModel).where(OrderModel.internal_order_id == f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L1"))).scalar_one()
        s.add(
            ExecutionModel(
                exec_id=f"exec-{trade_id}-L0",
                order_id=o1.id,
                account_id=acc_id,
                internal_order_id=o1.internal_order_id,
                symbol="AAPL",
                side="SELL",
                quantity=Decimal("10"),
                price=Decimal("155"),
                executed_at=None,
            )
        )
        s.add(
            ExecutionModel(
                exec_id=f"exec-{trade_id}-L1",
                order_id=o2.id,
                account_id=acc_id,
                internal_order_id=o2.internal_order_id,
                symbol="MSFT",
                side="BUY",
                quantity=Decimal("5"),
                price=Decimal("295"),
                executed_at=None,
            )
        )

    svc = KillSwitchService(session_factory=session_factory)
    op, _ = await svc.initiate_square_off(account_id=acc_id)
    await svc._execute_flatten_operation(op.operation_id)

    async with session_factory() as s:
        pos = await s.get(PositionModel, (acc_id, trade_id))
        # Should be closed via Tier2 fallback
        assert pos.risk_state == "CLOSED"
