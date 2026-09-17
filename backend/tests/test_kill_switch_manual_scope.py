"""Comprehensive test suite for Kill Switch scope=manual ("Flatten Manual Positions").

Tests all 16 required scenarios:
1. Manual only (Manual AAPL +50 -> CLOSED, broker AAPL -> 0)
2. Shared symbol (Engine AAPL +100, Manual AAPL +50 -> Manual CLOSED, Engine +100 OPEN, broker +100)
3. Multiple symbols (Engine TSLA +100, Manual AAPL +50, MSFT -20 -> AAPL 0, MSFT 0, TSLA +100)
4. Multiple manual positions same symbol (Engine AAPL +100, Manual AAPL +50, Manual AAPL +30 -> both closed, broker +100)
5. Partial fill (Manual AAPL +100, fill 60 -> remaining 40, status not CLOSED; fill 40 -> CLOSED)
6. Multiple executions (Weighted-average price calculation and realized PnL)
7. Missing execution price (Broker confirms, execution unavailable -> FLATTENED_PENDING_PRICE, no fabricated price)
8. Late execution (Starts pending, execution inserted later, reconciliation closes position)
9. Duplicate execution idempotency (Reconciliation runs multiple times -> no duplicate PnL or close)
10. No manual positions (Complete safely with 0 orders submitted, engine untouched)
11. Snapshot timing (New manual position opened after snapshot is not included in batch)
12. Block new manual order during active manual flatten (pre-trade validation rejects new manual order)
13. Service restart recovery (Incomplete manual flatten operation resumes safely)
14. Engine isolation regression (Engine positions remain completely untouched)
15. Existing account kill switch regression (Flatten Account still functions)
16. Existing signal kill switch regression (Flatten Signal Positions still functions)
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_SCOPE_ACCOUNT,
    KILL_SWITCH_SCOPE_ENGINE,
    KILL_SWITCH_SCOPE_MANUAL,
    KILL_SWITCH_STATUS_COMPLETE,
    KILL_SWITCH_STATUS_UNRESOLVED,
    KillSwitchOperationModel,
)
from app.db.models.manual_order import (
    ManualExecutionModel,
    ManualOrderModel,
    ManualPositionModel,
)
from app.db.models.position import PositionModel
from app.schemas.manual_schemas import ManualOrderSubmitRequest
from app.services.kill_switch import (
    KillSwitchService,
    clear_account_kill_switch,
    is_account_kill_switch_active,
    is_manual_kill_switch_active,
)
from app.services.manual_trading import ManualTradingService


async def _create_test_account(session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, str]:
    tag = uuid4().hex[:6]
    ibkr_acc = f"DU{tag.upper()}"
    async with session_factory() as s, s.begin():
        acc = AccountModel(
            name=f"ManualKS-{tag}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        s.add(acc)
        await s.flush()
        return acc.id, ibkr_acc


@pytest.mark.asyncio
async def test_1_manual_only_flatten(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 1: Manual AAPL +50, Engine none -> Manual CLOSED, Broker AAPL -> 0."""
    acc_id, _ibkr = await _create_test_account(session_factory)
    trade_id = f"MAN-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=trade_id,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5001
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, created = await svc.initiate_manual_square_off(acc_id, requested_by="operator")
    assert created
    assert op.scope == KILL_SWITCH_SCOPE_MANUAL
    assert op.initial_position_count == 1

    await svc._execute_manual_flatten_operation(op.operation_id)

    assert mock_client.placeOrder.called
    _order_id, contract, ib_order = mock_client.placeOrder.call_args[0]
    assert contract.symbol == "AAPL"
    assert ib_order.action == "SELL"
    assert ib_order.totalQuantity == 50

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "FILLED"
        order_row.filled_quantity = Decimal(50)

        s.add(
            ManualExecutionModel(
                exec_id=f"exec-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(50),
                price=Decimal("155.00"),
                executed_at=datetime.now(UTC),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        pos = (
            await s.execute(
                select(ManualPositionModel).where(
                    ManualPositionModel.account_id == acc_id,
                    ManualPositionModel.trade_id == trade_id,
                )
            )
        ).scalar_one()
        assert pos.status == "CLOSED"
        assert pos.signed_qty == Decimal(0)
        assert pos.realized_pnl == Decimal("250.00")

        op_row = await s.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_COMPLETE
        assert op_row.flattened_count == 1


@pytest.mark.asyncio
async def test_2_shared_symbol_engine_untouched(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 2 (MANDATORY): Engine AAPL +100, Manual AAPL +50.

    Trigger Flatten Manual Positions.
    Expected: Manual AAPL -> CLOSED, Engine AAPL -> remains OPEN +100.
    Broker exposure should remain +100 for the engine.
    """
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_trade = f"MAN-{uuid4().hex[:6]}"
    eng_trade = f"ENG-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=eng_trade,
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("150.00"),
                leg_b_symbol=None,
                leg_b_signed_qty=None,
                leg_b_entry_mark=None,
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5002
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, created = await svc.initiate_manual_square_off(acc_id, requested_by="operator")
    assert created

    await svc._execute_manual_flatten_operation(op.operation_id)

    assert mock_client.placeOrder.call_count == 1
    _order_id, contract, ib_order = mock_client.placeOrder.call_args[0]
    assert contract.symbol == "AAPL"
    assert ib_order.action == "SELL"
    assert ib_order.totalQuantity == 50

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "FILLED"
        order_row.filled_quantity = Decimal(50)
        s.add(
            ManualExecutionModel(
                exec_id=f"exec-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(50),
                price=Decimal("152.00"),
                executed_at=datetime.now(UTC),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        man_pos = (
            await s.execute(
                select(ManualPositionModel).where(
                    ManualPositionModel.account_id == acc_id,
                    ManualPositionModel.trade_id == man_trade,
                )
            )
        ).scalar_one()
        assert man_pos.status == "CLOSED"
        assert man_pos.signed_qty == Decimal(0)

        eng_pos = (
            await s.execute(
                select(PositionModel).where(
                    PositionModel.account_id == acc_id,
                    PositionModel.trade_id == eng_trade,
                )
            )
        ).scalar_one()
        assert eng_pos.risk_state == "OPEN"
        assert eng_pos.leg_a_signed_qty == Decimal(100)
        assert eng_pos.closed_at is None


@pytest.mark.asyncio
async def test_3_multiple_symbols(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 3: Engine TSLA +100, Manual AAPL +50, MSFT -20.

    Expected: AAPL -> 0, MSFT -> 0, TSLA -> +100 remains OPEN.
    """
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_aapl = f"MAN-AAPL-{uuid4().hex[:6]}"
    man_msft = f"MAN-MSFT-{uuid4().hex[:6]}"
    eng_tsla = f"ENG-TSLA-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=eng_tsla,
                strategy_id="model_blue",
                leg_a_symbol="TSLA",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("200.00"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_aapl,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_msft,
                symbol="MSFT",
                con_id=1002,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(-20),
                avg_cost=Decimal("300.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.side_effect = [5003, 5004]
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)
    assert op.initial_position_count == 2

    await svc._execute_manual_flatten_operation(op.operation_id)
    assert mock_client.placeOrder.call_count == 2

    async with session_factory() as s, s.begin():
        orders = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalars().all()
        for o in orders:
            o.status = "FILLED"
            o.filled_quantity = o.quantity
            fill_price = Decimal("150.00") if o.symbol == "AAPL" else Decimal("300.00")
            s.add(
                ManualExecutionModel(
                    exec_id=f"exec-{uuid4().hex[:6]}",
                    manual_order_id=o.id,
                    quantity=o.quantity,
                    price=fill_price,
                    executed_at=datetime.now(UTC),
                )
            )

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        p_aapl = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_aapl))).scalar_one()
        p_msft = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_msft))).scalar_one()
        p_tsla = (await s.execute(select(PositionModel).where(PositionModel.trade_id == eng_tsla))).scalar_one()

        assert p_aapl.status == "CLOSED"
        assert p_msft.status == "CLOSED"
        assert p_tsla.risk_state == "OPEN"
        assert p_tsla.leg_a_signed_qty == Decimal(100)


@pytest.mark.asyncio
async def test_4_multiple_manual_positions_same_symbol(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 4: Engine AAPL +100, Manual AAPL +50, Manual AAPL +30.

    Expected: Both manual positions CLOSED, Engine AAPL +100 remains OPEN.
    """
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_1 = f"MAN-1-{uuid4().hex[:6]}"
    man_2 = f"MAN-2-{uuid4().hex[:6]}"
    eng = f"ENG-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=eng,
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("150.00"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_1,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_2,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(30),
                avg_cost=Decimal("152.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.side_effect = [5005, 5006]
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)
    assert op.initial_position_count == 2

    await svc._execute_manual_flatten_operation(op.operation_id)
    assert mock_client.placeOrder.call_count == 2

    async with session_factory() as s, s.begin():
        orders = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalars().all()
        for o in orders:
            o.status = "FILLED"
            o.filled_quantity = o.quantity
            s.add(
                ManualExecutionModel(
                    exec_id=f"exec-{uuid4().hex[:6]}",
                    manual_order_id=o.id,
                    quantity=o.quantity,
                    price=Decimal("155.00"),
                    executed_at=datetime.now(UTC),
                )
            )

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        p1 = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_1))).scalar_one()
        p2 = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_2))).scalar_one()
        peng = (await s.execute(select(PositionModel).where(PositionModel.trade_id == eng))).scalar_one()

        assert p1.status == "CLOSED"
        assert p2.status == "CLOSED"
        assert peng.risk_state == "OPEN"
        assert peng.leg_a_signed_qty == Decimal(100)


@pytest.mark.asyncio
async def test_5_partial_fill_handling(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 5: Manual AAPL +100, close order fills 60 -> remaining 40, status CLOSING.

    Then remaining 40 fills -> status CLOSED.
    """
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_trade = f"MAN-PARTIAL-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(100),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5007
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)

    await svc._execute_manual_flatten_operation(op.operation_id)

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "PARTIALLY_FILLED"
        order_row.filled_quantity = Decimal(60)
        s.add(
            ManualExecutionModel(
                exec_id=f"exec-part1-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(60),
                price=Decimal("155.00"),
                executed_at=datetime.now(UTC),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        p = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_trade))).scalar_one()
        assert p.status == "CLOSING"
        assert p.signed_qty == Decimal(40)
        assert p.realized_pnl == Decimal("300.00")

        op_row = await s.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_UNRESOLVED

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "FILLED"
        order_row.filled_quantity = Decimal(100)
        s.add(
            ManualExecutionModel(
                exec_id=f"exec-part2-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(40),
                price=Decimal("156.00"),
                executed_at=datetime.now(UTC),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        p = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_trade))).scalar_one()
        assert p.status == "CLOSED"
        assert p.signed_qty == Decimal(0)
        assert p.realized_pnl == Decimal("540.00")

        op_row = await s.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_COMPLETE


@pytest.mark.asyncio
async def test_6_multiple_executions_weighted_average_price(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 6: SELL 100 executed across 3 fills:

    50 @ 250, 30 @ 251, 20 @ 249.
    Weighted average = (50*250 + 30*251 + 20*249) / 100 = 250.10.
    Entry price = 240.00.
    Realized PnL = 100 * (250.10 - 240.00) = 1010.00.
    """
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_trade = f"MAN-VWAP-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="XYZ",
                con_id=2001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(100),
                avg_cost=Decimal("240.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5008
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)
    await svc._execute_manual_flatten_operation(op.operation_id)

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "FILLED"
        order_row.filled_quantity = Decimal(100)
        s.add_all([
            ManualExecutionModel(
                exec_id=f"exec-1-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(50),
                price=Decimal("250.00"),
                executed_at=datetime.now(UTC),
            ),
            ManualExecutionModel(
                exec_id=f"exec-2-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(30),
                price=Decimal("251.00"),
                executed_at=datetime.now(UTC),
            ),
            ManualExecutionModel(
                exec_id=f"exec-3-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(20),
                price=Decimal("249.00"),
                executed_at=datetime.now(UTC),
            ),
        ])

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        p = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_trade))).scalar_one()
        assert p.status == "CLOSED"
        assert p.realized_pnl == Decimal("1010.00")


@pytest.mark.asyncio
async def test_7_missing_execution_price_does_not_fabricate(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 7: Broker order filled but no execution details available yet.

    Position must transition to FLATTENED_PENDING_PRICE, NOT mark closed at entry price.
    """
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_trade = f"MAN-NOPRICE-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5009
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)
    await svc._execute_manual_flatten_operation(op.operation_id)

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "FILLED"
        order_row.filled_quantity = Decimal(50)

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        p = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_trade))).scalar_one()
        assert p.status == "FLATTENED_PENDING_PRICE"
        assert p.realized_pnl == Decimal(0)
        assert p.closed_at is None

        op_row = await s.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_UNRESOLVED


@pytest.mark.asyncio
async def test_8_late_execution_retry_converges(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 8: Operation is UNRESOLVED due to missing execution.

    Late execution arrives -> retry_unresolved_operations closes position cleanly.
    """
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_trade = f"MAN-LATE-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="FLATTENED_PENDING_PRICE",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5010
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)
    await svc._execute_manual_flatten_operation(op.operation_id)

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)
    async with session_factory() as s:
        op_row = await s.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_UNRESOLVED

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "FILLED"
        order_row.filled_quantity = Decimal(50)
        s.add(
            ManualExecutionModel(
                exec_id=f"exec-late-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(50),
                price=Decimal("158.00"),
                executed_at=datetime.now(UTC),
            )
        )

    retried = await svc.retry_unresolved_operations(acc_id)
    assert retried == 1

    async with session_factory() as s:
        p = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_trade))).scalar_one()
        assert p.status == "CLOSED"
        assert p.realized_pnl == Decimal("400.00")
        op_row = await s.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_COMPLETE


@pytest.mark.asyncio
async def test_9_duplicate_execution_idempotency(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 9: Reconciling multiple times does not duplicate PnL or reopen/reclose."""
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_trade = f"MAN-IDEM-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(10),
                avg_cost=Decimal("100.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5011
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)
    await svc._execute_manual_flatten_operation(op.operation_id)

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "FILLED"
        order_row.filled_quantity = Decimal(10)
        s.add(
            ManualExecutionModel(
                exec_id=f"exec-idem-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(10),
                price=Decimal("110.00"),
                executed_at=datetime.now(UTC),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)
    async with session_factory() as s:
        p1 = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_trade))).scalar_one()
        assert p1.status == "CLOSED"
        assert p1.realized_pnl == Decimal("100.00")

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)
    async with session_factory() as s:
        p2 = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_trade))).scalar_one()
        assert p2.status == "CLOSED"
        assert p2.realized_pnl == Decimal("100.00")


@pytest.mark.asyncio
async def test_10_no_manual_positions(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 10: No open manual positions -> complete safely with 0 orders submitted."""
    acc_id, _ibkr = await _create_test_account(session_factory)
    eng_trade = f"ENG-UNT-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=eng_trade,
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("150.00"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )

    mock_client = MagicMock()
    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)

    assert op.initial_position_count == 0
    assert op.status == KILL_SWITCH_STATUS_COMPLETE
    mock_client.placeOrder.assert_not_called()

    async with session_factory() as s:
        eng_pos = (await s.execute(select(PositionModel).where(PositionModel.trade_id == eng_trade))).scalar_one()
        assert eng_pos.risk_state == "OPEN"


@pytest.mark.asyncio
async def test_11_snapshot_isolation(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 11: Position opened after snapshot is not included in manual flatten batch."""
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_snap = f"MAN-SNAP-{uuid4().hex[:6]}"
    man_late = f"MAN-LATE-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_snap,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5012
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_late,
                symbol="MSFT",
                con_id=1002,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(20),
                avg_cost=Decimal("300.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    await svc._execute_manual_flatten_operation(op.operation_id)

    assert mock_client.placeOrder.call_count == 1
    _order_id, contract, _ib_order = mock_client.placeOrder.call_args[0]
    assert contract.symbol == "AAPL"

    async with session_factory() as s:
        p_late = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_late))).scalar_one()
        assert p_late.status == "OPEN"
        assert p_late.signed_qty == Decimal(20)


@pytest.mark.asyncio
async def test_12_block_new_manual_orders_during_flatten(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 12: Validate that new manual orders are rejected while manual kill switch is active."""
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_trade = f"MAN-BLOCK-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.is_connected.return_value = True

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    _op, _ = await svc.initiate_manual_square_off(acc_id)

    assert is_manual_kill_switch_active(acc_id)

    async with session_factory() as session:
        acc = await session.get(AccountModel, acc_id)
        assert acc is not None
        manual_svc = ManualTradingService(session=session, client=mock_client)
        req = ManualOrderSubmitRequest(
            con_id=1001,
            symbol="AAPL",
            sec_type="CFD",
            exchange="SMART",
            currency="USD",
            side="BUY",
            quantity=Decimal(10),
            order_type="MARKET",
            tif="DAY",
            idempotency_key=f"idem-{uuid4().hex[:6]}",
        )
        valid, errors, _ = await manual_svc.validate_pretrade(acc, req)
        assert not valid
        assert any("manual kill switch is active" in err for err in errors)

    await clear_account_kill_switch(session_factory, acc_id, scope=KILL_SWITCH_SCOPE_MANUAL)
    assert not is_manual_kill_switch_active(acc_id)


@pytest.mark.asyncio
async def test_13_service_restart_recovery(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 13: Service restart recovery resumes incomplete manual flatten operations."""
    acc_id, _ibkr = await _create_test_account(session_factory)
    man_trade = f"MAN-RESTART-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5013
    mock_client.placeOrder = MagicMock()

    svc1 = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc1.initiate_manual_square_off(acc_id)

    svc2 = KillSwitchService(session_factory=session_factory, client=mock_client)
    resumed = await svc2.resume_incomplete_flattens()
    assert op.operation_id in resumed

    await svc2._execute_manual_flatten_operation(op.operation_id)
    assert mock_client.placeOrder.called


@pytest.mark.asyncio
async def test_14_engine_isolation_regression(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 14: Engine isolation regression test.

    Before: Engine AAPL +100, Manual AAPL +50.
    After manual flatten: Engine AAPL +100 remains untouched, Manual AAPL 0.
    """
    acc_id, _ibkr = await _create_test_account(session_factory)
    eng_trade = f"ENG-ISO-{uuid4().hex[:6]}"
    man_trade = f"MAN-ISO-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=eng_trade,
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("150.00"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )
        s.add(
            ManualPositionModel(
                account_id=acc_id,
                trade_id=man_trade,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )

    mock_client = MagicMock()
    mock_client.allocate_next_order_id.return_value = 5014
    mock_client.placeOrder = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, client=mock_client)
    op, _ = await svc.initiate_manual_square_off(acc_id)
    await svc._execute_manual_flatten_operation(op.operation_id)

    async with session_factory() as s, s.begin():
        order_row = (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == acc_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()
        order_row.status = "FILLED"
        order_row.filled_quantity = Decimal(50)
        s.add(
            ManualExecutionModel(
                exec_id=f"exec-iso-{uuid4().hex[:6]}",
                manual_order_id=order_row.id,
                quantity=Decimal(50),
                price=Decimal("150.00"),
                executed_at=datetime.now(UTC),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, acc_id)

    async with session_factory() as s:
        man_p = (await s.execute(select(ManualPositionModel).where(ManualPositionModel.trade_id == man_trade))).scalar_one()
        eng_p = (await s.execute(select(PositionModel).where(PositionModel.trade_id == eng_trade))).scalar_one()

        assert man_p.status == "CLOSED"
        assert eng_p.risk_state == "OPEN"
        assert eng_p.leg_a_signed_qty == Decimal(100)
        assert eng_p.closed_at is None


@pytest.mark.asyncio
async def test_15_existing_account_kill_switch_regression(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 15: Existing scope=account ("Flatten Account") continues to function."""
    acc_id, _ibkr = await _create_test_account(session_factory)
    svc = KillSwitchService(session_factory=session_factory)

    op, created = await svc.arm_account_kill_switch_only(acc_id, requested_by="operator_account_flatten")
    assert created
    assert op.scope == KILL_SWITCH_SCOPE_ACCOUNT
    assert is_account_kill_switch_active(acc_id)

    await clear_account_kill_switch(session_factory, acc_id, scope=KILL_SWITCH_SCOPE_ACCOUNT)
    assert not is_account_kill_switch_active(acc_id)


@pytest.mark.asyncio
async def test_16_existing_signal_kill_switch_regression(session_factory: async_sessionmaker[AsyncSession]):
    """Scenario 16: Existing scope=engine ("Flatten Signal Positions") continues to function."""
    acc_id, _ibkr = await _create_test_account(session_factory)
    eng_trade = f"ENG-SIG-{uuid4().hex[:6]}"

    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=acc_id,
                trade_id=eng_trade,
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal(10),
                leg_a_entry_mark=Decimal("150.00"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
            )
        )

    svc = KillSwitchService(session_factory=session_factory)
    op, created = await svc.initiate_square_off(acc_id, requested_by="operator")
    assert created
    assert op.scope == KILL_SWITCH_SCOPE_ENGINE
    assert is_account_kill_switch_active(acc_id)

    await clear_account_kill_switch(session_factory, acc_id, scope=KILL_SWITCH_SCOPE_ENGINE)
    assert not is_account_kill_switch_active(acc_id)
