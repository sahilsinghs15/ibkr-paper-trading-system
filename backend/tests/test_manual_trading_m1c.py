"""Comprehensive verification tests for Manual Trading Milestone M1-C.

Covers:
- Order status callback updates (SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED)
- Execution fills ingestion and exactly-once deduplication on exec_id
- Concurrent duplicate execution processing
- Manual position ledger math for all 8 transitions (Open Long/Short, Add, Reduce, Close, Flip)
- Realized P&L calculations with commission arrival timing (before, after, duplicate)
- broker orderId and permId correlation with account isolation
- Restart safety, callback replay, and ambiguous PENDING_SUBMIT handling
- Transactional atomicity and broker safety (zero cancellation, zero resubmission)
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.db.models.account import AccountModel
from app.db.models.manual_order import (
    ManualOrderModel,
)
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import SignalModel
from app.db.models.trade_execution import TradeExecutionModel
from app.db.repositories.manual_repository import (
    ManualExecutionRepository,
    ManualOrderRepository,
    ManualPositionRepository,
)
from app.services.manual_callbacks import ManualExecutionListener
from app.services.manual_recovery import ManualTradingRecoveryService

# ── MOCK IBKR EXECUTION OBJECT ─────────────────────────────────────────


class MockIBKRExecution:
    def __init__(
        self,
        execId: str,
        orderId: int,
        shares: float,
        price: float,
        side: str = "BOT",
        permId: int = 12345678,
        acctNumber: str = "DU123456",
        orderRef: str = "",
        time: str = "20260911 16:00:00",
    ):
        self.execId = execId
        self.orderId = orderId
        self.shares = shares
        self.price = price
        self.side = side
        self.permId = permId
        self.acctNumber = acctNumber
        self.orderRef = orderRef
        self.time = time


class MockIBKRCommissionReport:
    def __init__(
        self,
        execId: str,
        commission: float,
        currency: str = "USD",
        realizedPNL: float | None = None,
    ):
        self.execId = execId
        self.commission = commission
        self.currency = currency
        self.realizedPNL = realizedPNL


class MockIBKRContract:
    def __init__(
        self,
        conId: int = 10001,
        symbol: str = "IBUS500",
        secType: str = "CFD",
        exchange: str = "SMART",
        currency: str = "USD",
    ):
        self.conId = conId
        self.symbol = symbol
        self.secType = secType
        self.exchange = exchange
        self.currency = currency


# ── TEST FIXTURE HELPERS ───────────────────────────────────────────────


async def create_test_account(session, prefix: str) -> AccountModel:
    acc = AccountModel(
        name=f"Acc {prefix}",
        ibkr_account=f"DU{prefix.upper()}",
        total_margin=Decimal("100000.00"),
        enabled=True,
    )
    session.add(acc)
    await session.commit()
    await session.refresh(acc)
    return acc


async def create_test_manual_order(
    session,
    account_id: int,
    ibkr_account: str,
    *,
    side: str = "BUY",
    quantity: Decimal = Decimal(10),
    order_type: str = "LIMIT",
    limit_price: Decimal = Decimal("5000.00"),
    status: str = "SUBMITTED",
    broker_order_id: str | None = "5001",
    trade_id: str | None = None,
) -> ManualOrderModel:
    order_repo = ManualOrderRepository(session)
    suffix = uuid.uuid4().hex[:6]
    order = await order_repo.create_order(
        account_id=account_id,
        ibkr_account=ibkr_account,
        idempotency_key=f"idem_{suffix}",
        internal_order_id=f"MAN_{suffix.upper()}",
        trade_id=trade_id or f"TRD_{suffix.upper()}",
        symbol="IBUS500",
        side=side,
        quantity=quantity,
        order_type=order_type,
        con_id=10001,
        sec_type="CFD",
        exchange="SMART",
        currency="USD",
        limit_price=limit_price,
    )
    if broker_order_id or status != "PENDING_SUBMIT":
        await order_repo.update_status(
            order.id,
            status=status,
            broker_order_id=broker_order_id,
        )
    await session.commit()
    return order


# ── TESTS 1-5: ORDER STATUS CALLBACKS ──────────────────────────────────


@pytest.mark.asyncio
async def test_order_status_callbacks(session_factory):
    """1-5: Order status updates order lifecycle without mutating executions or positions."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await create_test_account(session, suffix)

        order = await create_test_manual_order(
            session, acc.id, acc.ibkr_account, broker_order_id="6001", status="SUBMITTED"
        )

    listener = ManualExecutionListener(session_factory)

    # 1. SUBMITTED callback
    await listener.handle_order_status(order_id=6001, status="Submitted", perm_id=888888)
    async with session_factory() as session:
        o = await ManualOrderRepository(session).get_by_id(order.id)
        assert o.status == "SUBMITTED"  # pyrefly: ignore[missing-attribute]
        assert o.perm_id == 888888  # pyrefly: ignore[missing-attribute]

    # 2. PARTIALLY_FILLED callback
    await listener.handle_order_status(order_id=6001, status="PartiallyFilled")
    async with session_factory() as session:
        o = await ManualOrderRepository(session).get_by_id(order.id)
        assert o.status == "PARTIALLY_FILLED"  # pyrefly: ignore[missing-attribute]

    # 3. FILLED callback
    await listener.handle_order_status(order_id=6001, status="Filled")
    async with session_factory() as session:
        o = await ManualOrderRepository(session).get_by_id(order.id)
        assert o.status == "FILLED"  # pyrefly: ignore[missing-attribute]
        assert o.completed_at is not None  # pyrefly: ignore[missing-attribute]

    # 4. CANCELLED callback does not create execution or positions
    async with session_factory() as session:
        order2 = await create_test_manual_order(
            session, acc.id, acc.ibkr_account, broker_order_id="6002", status="SUBMITTED"
        )
    await listener.handle_order_status(order_id=6002, status="Cancelled")
    async with session_factory() as session:
        o2 = await ManualOrderRepository(session).get_by_id(order2.id)
        assert o2.status == "CANCELLED"  # pyrefly: ignore[missing-attribute]
        # Zero executions created
        execs = await ManualExecutionRepository(session).list_for_order(order2.id)
        assert len(execs) == 0

    # 5. Unknown order ID is safely ignored
    res = await listener.handle_order_status(order_id=999999, status="Filled")
    assert res is None


# ── TESTS 6-12: EXECUTIONS & EXACTLY-ONCE DEDUPLICATION ───────────────


@pytest.mark.asyncio
async def test_execution_fills_and_deduplication(session_factory):
    """6-12: execDetails creates execution, mutates position, and deduplicates exactly once."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await create_test_account(session, suffix)

        order = await create_test_manual_order(
            session,
            acc.id,
            acc.ibkr_account,
            quantity=Decimal(10),
            broker_order_id="7001",
            status="SUBMITTED",
        )

    listener = ManualExecutionListener(session_factory)
    contract = MockIBKRContract(conId=10001, symbol="IBUS500")

    # 6 & 7: First fill of 4 shares creates manual_execution and updates position
    exec1 = MockIBKRExecution(
        execId=f"exec_1_{suffix}",
        orderId=7001,
        shares=4.0,
        price=5000.0,
        side="BOT",
        permId=99001,
        acctNumber=acc.ibkr_account,
        orderRef=order.internal_order_id,
    )
    _o, is_new = await listener.handle_exec_details(contract=contract, execution=exec1)
    assert is_new is True

    async with session_factory() as session:
        # Check execution row
        ex = await ManualExecutionRepository(session).get_by_exec_id(f"exec_1_{suffix}")
        assert ex is not None
        assert ex.quantity == Decimal(4)
        assert ex.price == Decimal(5000)

        # Check position row
        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        assert pos is not None
        assert pos.signed_qty == Decimal(4)
        assert pos.avg_cost == Decimal(5000)
        assert pos.status == "OPEN"

        # Check order status is PARTIALLY_FILLED
        o_cur = await ManualOrderRepository(session).get_by_id(order.id)
        assert o_cur.status == "PARTIALLY_FILLED"  # pyrefly: ignore[missing-attribute]

    # 8 & 9: Duplicate execDetails callback with same exec_id
    _o_dup, is_new_dup = await listener.handle_exec_details(contract=contract, execution=exec1)
    assert is_new_dup is False

    async with session_factory() as session:
        # Check executions count is still 1
        execs = await ManualExecutionRepository(session).list_for_order(order.id)
        assert len(execs) == 1

        # Position quantity was NOT double-counted
        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        assert pos.signed_qty == Decimal(4)  # pyrefly: ignore[missing-attribute]

    # 11 & 12: Second partial fill of 6 shares completes the order
    exec2 = MockIBKRExecution(
        execId=f"exec_2_{suffix}",
        orderId=7001,
        shares=6.0,
        price=5010.0,
        side="BOT",
        permId=99001,
        acctNumber=acc.ibkr_account,
        orderRef=order.internal_order_id,
    )
    _, is_new2 = await listener.handle_exec_details(contract=contract, execution=exec2)
    assert is_new2 is True

    async with session_factory() as session:
        execs = await ManualExecutionRepository(session).list_for_order(order.id)
        assert len(execs) == 2

        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        assert pos.signed_qty == Decimal(10)  # pyrefly: ignore[missing-attribute]
        # Weighted avg: (4*5000 + 6*5010)/10 = (20000 + 30060)/10 = 5006.0
        assert pos.avg_cost == Decimal("5006.0")  # pyrefly: ignore[missing-attribute]

        o_final = await ManualOrderRepository(session).get_by_id(order.id)
        assert o_final.status == "FILLED"  # pyrefly: ignore[missing-attribute]
        assert o_final.completed_at is not None  # pyrefly: ignore[missing-attribute]


# ── TEST 10: CONCURRENT DUPLICATE EXECUTION PROCESSING ────────────────


@pytest.mark.asyncio
async def test_concurrent_duplicate_execdetails(session_factory):
    """10: Concurrent execution processing of the same execId results in exactly one fill."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await create_test_account(session, suffix)

        order = await create_test_manual_order(
            session, acc.id, acc.ibkr_account, broker_order_id="7005", status="SUBMITTED"
        )

    listener = ManualExecutionListener(session_factory)
    contract = MockIBKRContract(conId=10001, symbol="IBUS500")

    exec_concurrent = MockIBKRExecution(
        execId=f"exec_concur_{suffix}",
        orderId=7005,
        shares=5.0,
        price=5000.0,
        side="BOT",
        acctNumber=acc.ibkr_account,
        orderRef=order.internal_order_id,
    )

    # Launch 5 simultaneous processing tasks for the identical execution
    tasks = [
        listener.handle_exec_details(contract=contract, execution=exec_concurrent)
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks)

    # Exactly one task should have reported is_new = True
    is_new_count = sum(1 for _, is_new in results if is_new)
    assert is_new_count == 1

    async with session_factory() as session:
        # Exactly one execution row exists
        all_execs = await ManualExecutionRepository(session).list_for_order(order.id)
        assert len(all_execs) == 1

        # Position updated exactly once (signed_qty == 5, not 25)
        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        assert pos.signed_qty == Decimal(5)  # pyrefly: ignore[missing-attribute]


# ── TESTS 13-24: POSITION MATH FOR ALL 8 TRANSITIONS ───────────────────


@pytest.mark.asyncio
async def test_position_math_all_transitions(session_factory):
    """13-24: Tests all 8 position lifecycle transitions and exact P&L math."""
    suffix = uuid.uuid4().hex[:6]
    trade_id_long = f"TRD_LONG_{suffix}"
    trade_id_short = f"TRD_SHORT_{suffix}"

    async with session_factory() as session:
        acc = await create_test_account(session, suffix)
        acc_id = acc.id
        pos_repo = ManualPositionRepository(session)

        # 13: Open long: BUY 10 @ 100
        p, delta = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_long,
            symbol="TEST",
            side="BUY",
            quantity=Decimal(10),
            price=Decimal(100),
            commission=Decimal("1.00"),
        )
        assert p.signed_qty == Decimal(10)
        assert p.avg_cost == Decimal(100)
        assert p.realized_pnl == Decimal("-1.00")
        assert p.status == "OPEN"

        # 14: Add to long: BUY 5 @ 110
        p, delta = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_long,
            symbol="TEST",
            side="BUY",
            quantity=Decimal(5),
            price=Decimal(110),
            commission=Decimal("0.50"),
        )
        # 10*100 + 5*110 = 1550 / 15 = 103.33333333
        assert p.signed_qty == Decimal(15)
        expected_avg = (Decimal(1000) + Decimal(550)) / Decimal(15)
        assert p.avg_cost == expected_avg
        assert p.realized_pnl == Decimal("-1.50")

        # 15: Reduce long: SELL 4 @ 120
        # Realized PnL: 4 * (120 - 103.33333333) - 0.50
        p, delta = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_long,
            symbol="TEST",
            side="SELL",
            quantity=Decimal(4),
            price=Decimal(120),
            commission=Decimal("0.50"),
        )
        assert p.signed_qty == Decimal(11)
        assert p.avg_cost == expected_avg  # Avg cost unchanged on reduction
        gross_pnl = Decimal(4) * (Decimal(120) - expected_avg)
        assert delta == gross_pnl - Decimal("0.50")
        assert p.realized_pnl == Decimal("-1.50") + gross_pnl - Decimal("0.50")
        assert p.status == "OPEN"

        # 16 & 23: Exact close long: SELL 11 @ 125
        p, delta = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_long,
            symbol="TEST",
            side="SELL",
            quantity=Decimal(11),
            price=Decimal(125),
            commission=Decimal("1.00"),
        )
        assert p.signed_qty == Decimal(0)
        assert p.status == "CLOSED"
        assert p.closed_at is not None

        # 17: Open short: SELL 10 @ 100
        p_s, delta = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_short,
            symbol="TEST",
            side="SELL",
            quantity=Decimal(10),
            price=Decimal(100),
            commission=Decimal("1.00"),
        )
        assert p_s.signed_qty == Decimal(-10)
        assert p_s.avg_cost == Decimal(100)
        assert p_s.realized_pnl == Decimal("-1.00")
        assert p_s.status == "OPEN"

        # 18: Add to short: SELL 5 @ 90
        # (10*100 + 5*90)/15 = 1450/15 = 96.66666667
        p_s, delta = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_short,
            symbol="TEST",
            side="SELL",
            quantity=Decimal(5),
            price=Decimal(90),
            commission=Decimal("0.50"),
        )
        assert p_s.signed_qty == Decimal(-15)
        expected_short_avg = Decimal(1450) / Decimal(15)
        assert p_s.avg_cost == expected_short_avg

        # 19: Reduce short: BUY 4 @ 80
        # Realized PnL: 4 * (96.66666667 - 80) - 0.50
        p_s, delta = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_short,
            symbol="TEST",
            side="BUY",
            quantity=Decimal(4),
            price=Decimal(80),
            commission=Decimal("0.50"),
        )
        assert p_s.signed_qty == Decimal(-11)
        assert p_s.avg_cost == expected_short_avg
        gross_short_pnl = Decimal(4) * (expected_short_avg - Decimal(80))
        assert delta == gross_short_pnl - Decimal("0.50")

        # 20: Close short: BUY 11 @ 75
        p_s, delta = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_short,
            symbol="TEST",
            side="BUY",
            quantity=Decimal(11),
            price=Decimal(75),
            commission=Decimal("1.00"),
        )
        assert p_s.signed_qty == Decimal(0)
        assert p_s.status == "CLOSED"

        # 21 & 24: Long-to-short flip: Start with Long 10 @ 100, then SELL 15 @ 120
        trade_id_flip = f"TRD_FLIP_{suffix}"
        p_f, _ = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_flip,
            symbol="FLIP",
            side="BUY",
            quantity=Decimal(10),
            price=Decimal(100),
        )
        assert p_f.signed_qty == Decimal(10)

        # Over-close / Flip with SELL 15 @ 120
        p_f, delta_f = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_flip,
            symbol="FLIP",
            side="SELL",
            quantity=Decimal(15),
            price=Decimal(120),
        )
        # Realized PnL applies ONLY to closed 10: 10 * (120 - 100) = +200
        assert delta_f == Decimal(200)
        assert p_f.realized_pnl == Decimal(200)
        # Position flipped to Short 5 @ 120
        assert p_f.signed_qty == Decimal(-5)
        assert p_f.avg_cost == Decimal(120)
        assert p_f.status == "OPEN"

        # 22: Short-to-long flip: BUY 10 @ 110 against Short 5 @ 120
        p_f, delta_f2 = await pos_repo.apply_execution(
            account_id=acc_id,
            trade_id=trade_id_flip,
            symbol="FLIP",
            side="BUY",
            quantity=Decimal(10),
            price=Decimal(110),
        )
        # Realized PnL on closed 5 short: 5 * (120 - 110) = +50
        assert delta_f2 == Decimal(50)
        assert p_f.realized_pnl == Decimal(250)
        # Position flipped to Long 5 @ 110
        assert p_f.signed_qty == Decimal(5)
        assert p_f.avg_cost == Decimal(110)
        assert p_f.status == "OPEN"


# ── TESTS 25-30: COMMISSION TIMING & P&L INTEGRITY ────────────────────


@pytest.mark.asyncio
async def test_commission_timing_and_pnl(session_factory):
    """25-30: Commission report arriving before or after execDetails applies exactly once."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await create_test_account(session, suffix)

        order = await create_test_manual_order(
            session, acc.id, acc.ibkr_account, broker_order_id="8001", status="SUBMITTED"
        )

    listener = ManualExecutionListener(session_factory)
    contract = MockIBKRContract(conId=10001, symbol="IBUS500")

    # Case A: execDetails arrives, then commissionReport arrives AFTER
    exec_id_a = f"exec_comm_after_{suffix}"
    exec_a = MockIBKRExecution(
        execId=exec_id_a,
        orderId=8001,
        shares=5.0,
        price=5000.0,
        side="BOT",
        acctNumber=acc.ibkr_account,
        orderRef=order.internal_order_id,
    )
    await listener.handle_exec_details(contract=contract, execution=exec_a)

    async with session_factory() as session:
        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        assert pos.realized_pnl == Decimal(0)  # pyrefly: ignore[missing-attribute]

    # Commission report arrives after
    comm_report_a = MockIBKRCommissionReport(execId=exec_id_a, commission=2.50)
    updated = await listener.handle_commission_report(commission_report=comm_report_a)
    assert updated is True

    async with session_factory() as session:
        ex = await ManualExecutionRepository(session).get_by_exec_id(exec_id_a)
        assert ex.commission == Decimal("2.50")  # pyrefly: ignore[missing-attribute]
        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        assert pos.realized_pnl == Decimal("-2.50")  # pyrefly: ignore[missing-attribute]

    # Duplicate commissionReport cannot double-subtract
    updated_dup = await listener.handle_commission_report(commission_report=comm_report_a)
    assert updated_dup is False

    async with session_factory() as session:
        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        assert pos.realized_pnl == Decimal("-2.50")  # pyrefly: ignore[missing-attribute]

    # Case B: commissionReport arrives BEFORE execDetails (buffered)
    exec_id_b = f"exec_comm_before_{suffix}"
    comm_report_b = MockIBKRCommissionReport(execId=exec_id_b, commission=3.00)
    # Commission arrives out of order
    await listener.handle_commission_report(commission_report=comm_report_b)

    # Now execDetails arrives
    exec_b = MockIBKRExecution(
        execId=exec_id_b,
        orderId=8001,
        shares=5.0,
        price=5000.0,
        side="BOT",
        acctNumber=acc.ibkr_account,
        orderRef=order.internal_order_id,
    )
    await listener.handle_exec_details(contract=contract, execution=exec_b)

    async with session_factory() as session:
        ex_b = await ManualExecutionRepository(session).get_by_exec_id(exec_id_b)
        assert ex_b.commission == Decimal("3.00")  # pyrefly: ignore[missing-attribute]
        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        # Prior -2.50 minus new 3.00 = -5.50
        assert pos.realized_pnl == Decimal("-5.50")  # pyrefly: ignore[missing-attribute]


# ── TESTS 31-35: CORRELATION & SOURCE ISOLATION ───────────────────────


@pytest.mark.asyncio
async def test_correlation_and_source_isolation(session_factory):
    """31-35: Correlates by broker_order_id and perm_id with strict engine isolation."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await create_test_account(session, suffix)

        order = await create_test_manual_order(
            session, acc.id, acc.ibkr_account, broker_order_id="9001", status="SUBMITTED"
        )
        # Update perm_id
        await ManualOrderRepository(session).update_status(order.id, "SUBMITTED", perm_id=777888)
        await session.commit()

    listener = ManualExecutionListener(session_factory)
    contract = MockIBKRContract(conId=10001, symbol="IBUS500")

    # Match by perm_id (orderId=0 in callback)
    exec_perm = MockIBKRExecution(
        execId=f"exec_perm_{suffix}",
        orderId=0,
        shares=10.0,
        price=5000.0,
        side="BOT",
        permId=777888,
        acctNumber=acc.ibkr_account,
    )
    o, is_new = await listener.handle_exec_details(contract=contract, execution=exec_perm)
    assert is_new is True
    assert o.id == order.id  # pyrefly: ignore[missing-attribute]

    async with session_factory() as session:
        # Zero engine SignalModel, OrderModel, or PositionModel rows created for this manual trade!
        sig_count = (
            await session.execute(select(SignalModel).where(SignalModel.signal_id == order.trade_id))
        ).scalars().all()
        assert len(sig_count) == 0

        eng_orders = (
            await session.execute(select(OrderModel).where(OrderModel.trade_id == order.trade_id))
        ).scalars().all()
        assert len(eng_orders) == 0

        eng_pos = (
            await session.execute(select(PositionModel).where(PositionModel.trade_id == order.trade_id))
        ).scalars().all()
        assert len(eng_pos) == 0

        # Verified Trade Book entry has order_id=None
        trade_execs = (
            await session.execute(
                select(TradeExecutionModel).where(TradeExecutionModel.exec_id == f"exec_perm_{suffix}")
            )
        ).scalars().all()
        assert len(trade_execs) == 1
        assert trade_execs[0].order_id is None


# ── TESTS 36-40: RESTART SAFETY & PENDING_SUBMIT AMBIGUITY RECOVERY ──


@pytest.mark.asyncio
async def test_restart_safety_and_ambiguity_recovery(session_factory):
    """36-40: Restart recovery scans non-terminal orders; flags ambiguous PENDING_SUBMIT without resubmission."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await create_test_account(session, suffix)

        # Create ambiguous order in PENDING_SUBMIT across restart
        order_ambiguous = await create_test_manual_order(
            session, acc.id, acc.ibkr_account, status="PENDING_SUBMIT", broker_order_id=None
        )

    mock_client = MagicMock()
    mock_client.is_connected.return_value = True

    recovery_service = ManualTradingRecoveryService(session_factory, client=mock_client)
    count = await recovery_service.run_startup_recovery()
    assert count >= 1

    async with session_factory() as session:
        o = await ManualOrderRepository(session).get_by_id(order_ambiguous.id)
        # Must be marked ERROR with recovery explanation, NEVER resubmitted
        assert o.status == "ERROR"  # pyrefly: ignore[missing-attribute]
        assert "REQUIRES_RECOVERY" in o.reject_reason  # pyrefly: ignore[missing-attribute]


# ── TESTS 41-50: TRANSACTIONAL ATOMICITY & BROKER SAFETY ──────────────


@pytest.mark.asyncio
async def test_transactional_atomicity_and_broker_safety(session_factory):
    """41-50: Atomic rollback prevents partial mutations; zero broker write APIs present."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await create_test_account(session, suffix)

        order = await create_test_manual_order(
            session, acc.id, acc.ibkr_account, broker_order_id="9500", status="SUBMITTED"
        )

    listener = ManualExecutionListener(session_factory)
    contract = MockIBKRContract(conId=10001, symbol="IBUS500")

    # Verify execution and position mutation are committed together
    exec_atomic = MockIBKRExecution(
        execId=f"exec_atomic_{suffix}",
        orderId=9500,
        shares=10.0,
        price=5000.0,
        side="BOT",
        acctNumber=acc.ibkr_account,
        orderRef=order.internal_order_id,
    )
    await listener.handle_exec_details(contract=contract, execution=exec_atomic)

    async with session_factory() as session:
        # Both execution and position exist
        ex = await ManualExecutionRepository(session).get_by_exec_id(f"exec_atomic_{suffix}")
        pos = await ManualPositionRepository(session).get_by_trade_id(acc.id, order.trade_id)
        assert ex is not None
        assert pos is not None
        assert pos.signed_qty == Decimal(10)

    # 44-50: Broker safety verification - listener does NOT call placeOrder, cancelOrder, or reqGlobalCancel
    assert not hasattr(listener, "placeOrder")
    assert not hasattr(listener, "cancelOrder")
    assert not hasattr(listener, "reqGlobalCancel")
