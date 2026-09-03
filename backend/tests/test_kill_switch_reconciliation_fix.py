"""Targeted regression and reconciliation tests for Kill Switch P0 Position Persistence Fix."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_COMPLETE,
    KILL_SWITCH_STATUS_UNRESOLVED,
    KillSwitchOperationModel,
)
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import SignalModel
from app.db.repositories.position_repository import (
    RISK_STATE_CLOSED,
    RISK_STATE_OPEN,
    PositionRepository,
)
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.oms.models import ExecutionResult, OMSOrder, OMSOrderStatus
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide
from app.rms.models import OrderSide as RMSOrderSide
from app.services.kill_switch import KillSwitchService


@pytest.mark.asyncio
async def test_kill_switch_full_close_persists_closed_at_and_completes_op(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify filled emergency close orders update PositionModel.closed_at and set operation COMPLETE."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-AAPL-MSFT-TEST-{test_id}"

    # Setup Account and Position
    async with session_factory() as session, session.begin():
        acc = AccountModel(name="KSTestAcc1", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        trade = OpenModelBlueTrade(
            trade_id=trade_id,
            strategy_id="model_blue",
            direction=1,
            legs=(
                OpenModelBlueTradeLeg(symbol="AAPL", instrument_type="STK", side=OrderSide.BUY, quantity=Decimal("100.00"), price=Decimal("150.00")),
                OpenModelBlueTradeLeg(symbol="MSFT", instrument_type="STK", side=OrderSide.SELL, quantity=Decimal("50.00"), price=Decimal("300.00")),
            ),
        )
        await PositionRepository(session).open_trade(
            trade, account_id=acc_id, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60
        )

    leg1 = OrderLeg(symbol="AAPL", side=RMSOrderSide.SELL, quantity=100.0, price=Decimal("155.00"), contract_month="202612", leg_index=0)
    leg2 = OrderLeg(symbol="MSFT", side=RMSOrderSide.BUY, quantity=50.0, price=Decimal("295.00"), contract_month="202612", leg_index=1)
    close_intent = OrderIntent(signal_id=f"KILLSWITCH-{trade_id}", strategy_id="model_blue", action=OrderAction.CLOSE, legs=[leg1, leg2], account_id=acc_id)

    order1 = OMSOrder(internal_order_id=f"ORD-KS1-{test_id}-L0", intent=close_intent, leg_index=0, symbol="AAPL", side=RMSOrderSide.SELL, quantity=100.0, status=OMSOrderStatus.FILLED, filled_quantity=100.0, average_fill_price=Decimal("155.00"))
    order2 = OMSOrder(internal_order_id=f"ORD-KS1-{test_id}-L1", intent=close_intent, leg_index=1, symbol="MSFT", side=RMSOrderSide.BUY, quantity=50.0, status=OMSOrderStatus.FILLED, filled_quantity=50.0, average_fill_price=Decimal("295.00"))

    mock_baskets = MagicMock()
    mock_baskets.execute = AsyncMock(return_value=ExecutionResult(order=order1, orders=[order1, order2], rms_result=MagicMock(), success=True))
    mock_baskets._event = AsyncMock()

    mock_om = MagicMock()
    mock_om._baskets = mock_baskets
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda intent: intent)

    svc = KillSwitchService(session_factory=session_factory, order_manager=mock_om)
    op, created = await svc.initiate_square_off(account_id=acc_id)
    assert created is True

    # Await background execution task explicitly
    await svc._execute_flatten_operation(op.operation_id)

    # Verify PositionModel is CLOSED with closed_at set in PostgreSQL
    async with session_factory() as session:
        pos = await PositionRepository(session).get_by_trade_id(trade_id, account_id=acc_id)
        assert pos is not None
        assert pos.risk_state == RISK_STATE_CLOSED
        assert pos.closed_at is not None

        op_row = await session.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_COMPLETE
        assert op_row.unresolved_count == 0


@pytest.mark.asyncio
async def test_kill_switch_auto_repairs_stale_positions(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify _reconcile_and_finalize auto-repairs stale positions with filled close orders in DB."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-NOBL-SPY-TEST-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="KSTestAcc2", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        sig = SignalModel(
            signal_id=f"SIG-{test_id}",
            strategy_id="model_blue",
            action="CLOSE",
            pair="NOBL-SPY",
            side="SELL",
            ref_price_a=Decimal("90.00"),
            ref_price_b=Decimal("500.00"),
            status="ACCEPTED",
            raw_payload={"trade_id": trade_id},
        )
        session.add(sig)
        await session.flush()
        sig_id = sig.id

        pos_row = PositionModel(
            trade_id=trade_id,
            strategy_id="model_blue",
            account_id=acc_id,
            leg_a_symbol="NOBL",
            leg_a_signed_qty=Decimal("400.00"),
            leg_a_entry_mark=Decimal("90.00"),
            leg_b_symbol="SPY",
            leg_b_signed_qty=Decimal("-20.00"),
            leg_b_entry_mark=Decimal("500.00"),
            target=Decimal("0.05"),
            stop=Decimal("0.02"),
            time_limit=60,
            risk_state=RISK_STATE_OPEN,
            closed_at=None,
        )
        session.add(pos_row)

        o1 = OrderModel(
            signal_id=sig_id,
            account_id=acc_id,
            strategy_id="model_blue",
            leg="L0",
            symbol="NOBL",
            ibkr_contract="STK",
            buy_sell="SELL",
            quantity=Decimal("400.00"),
            limit_price=Decimal("92.00"),
            status="FILLED",
            trade_id=trade_id,
            internal_order_id=f"KILLSWITCH-{trade_id}-L0",
            fill_price=Decimal("92.00"),
            fill_qty=Decimal("400.00"),
        )
        o2 = OrderModel(
            signal_id=sig_id,
            account_id=acc_id,
            strategy_id="model_blue",
            leg="L1",
            symbol="SPY",
            ibkr_contract="STK",
            buy_sell="BUY",
            quantity=Decimal("20.00"),
            limit_price=Decimal("495.00"),
            status="FILLED",
            trade_id=trade_id,
            internal_order_id=f"KILLSWITCH-{trade_id}-L1",
            fill_price=Decimal("495.00"),
            fill_qty=Decimal("20.00"),
        )
        session.add_all([o1, o2])

    svc = KillSwitchService(session_factory=session_factory)
    op, _created = await svc.initiate_square_off(account_id=acc_id)

    # Await background execution task explicitly
    await svc._execute_flatten_operation(op.operation_id)

    # Verify auto-repair updated position closed_at and set operation COMPLETE
    async with session_factory() as session:
        pos = await PositionRepository(session).get_by_trade_id(trade_id, account_id=acc_id)
        assert pos is not None
        assert pos.risk_state == RISK_STATE_CLOSED
        assert pos.closed_at is not None

        op_row = await session.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_COMPLETE
        assert op_row.unresolved_count == 0


@pytest.mark.asyncio
async def test_kill_switch_rejected_close_leaves_position_open(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify rejected close order leaves position OPEN and sets operation UNRESOLVED."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-XLF-XLI-REJECT-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="KSTestAcc3", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        trade = OpenModelBlueTrade(
            trade_id=trade_id,
            strategy_id="model_blue",
            direction=1,
            legs=(
                OpenModelBlueTradeLeg(symbol="XLF", instrument_type="STK", side=OrderSide.BUY, quantity=Decimal("100.00"), price=Decimal("40.00")),
                OpenModelBlueTradeLeg(symbol="XLI", instrument_type="STK", side=OrderSide.SELL, quantity=Decimal("50.00"), price=Decimal("120.00")),
            ),
        )
        await PositionRepository(session).open_trade(
            trade, account_id=acc_id, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60
        )

    leg1 = OrderLeg(symbol="XLF", side=RMSOrderSide.SELL, quantity=100.0, price=Decimal("40.00"), contract_month="202612", leg_index=0)
    close_intent = OrderIntent(signal_id=f"KILLSWITCH-{trade_id}", strategy_id="model_blue", action=OrderAction.CLOSE, legs=[leg1], account_id=acc_id)
    order1 = OMSOrder(internal_order_id=f"ORD-REJ-{test_id}", intent=close_intent, leg_index=0, symbol="XLF", side=RMSOrderSide.SELL, quantity=100.0, status=OMSOrderStatus.REJECTED)

    mock_baskets = MagicMock()
    mock_baskets.execute = AsyncMock(return_value=ExecutionResult(order=order1, orders=[order1], rms_result=MagicMock(), success=False, error_message="Order Rejected by Broker"))
    mock_baskets._event = AsyncMock()

    mock_om = MagicMock()
    mock_om._baskets = mock_baskets
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda intent: intent)

    svc = KillSwitchService(session_factory=session_factory, order_manager=mock_om)
    op, _created = await svc.initiate_square_off(account_id=acc_id)

    # Await background execution task explicitly
    await svc._execute_flatten_operation(op.operation_id)

    # Verify position is STILL OPEN and operation status is UNRESOLVED
    async with session_factory() as session:
        pos = await PositionRepository(session).get_by_trade_id(trade_id, account_id=acc_id)
        assert pos is not None
        assert pos.risk_state == RISK_STATE_OPEN
        assert pos.closed_at is None

        op_row = await session.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_UNRESOLVED
        assert op_row.unresolved_count == 1
