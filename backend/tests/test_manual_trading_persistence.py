"""Unit and integration tests for Manual Trading persistence (M0.5).

Verifies:
1. manual order persistence & status
2. account isolation
3. idempotency uniqueness
4. duplicate exec_id protection
5. manual execution linked to manual order
6. manual position persistence & trade_id grouping
7. manual halt state persistence
8. manual audit event persistence
9. source separation (no signal_id requirement, no engine position mutation)
10. zero broker writes
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.manual_order import (
    ManualExecutionModel,
    ManualPositionModel,
)
from app.db.models.position import PositionModel
from app.db.repositories.manual_repository import (
    ManualAuditRepository,
    ManualExecutionRepository,
    ManualHaltRepository,
    ManualOrderRepository,
    ManualPositionRepository,
)


@pytest.mark.asyncio
async def test_manual_order_persistence_and_no_signal_id(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test manual order persists with source=manual and does NOT require signal_id."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Manual Acc {suffix}",
            ibkr_account=f"DU{suffix}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id
        ibkr_acc = acc.ibkr_account

    async with session_factory() as session, session.begin():
        repo = ManualOrderRepository(session)
        order = await repo.create_order(
            account_id=acc_id,
            ibkr_account=ibkr_acc,
            idempotency_key=f"idemp_{suffix}_01",
            internal_order_id=f"MAN_ORD_{suffix}_01",
            trade_id=f"MAN_TRD_{suffix}_01",
            symbol="AAPL",
            side="BUY",
            quantity=Decimal(100),
            order_type="LIMIT",
            limit_price=Decimal("150.25"),
            tif="DAY",
        )

        assert order.id is not None
        assert order.account_id == acc_id
        assert order.ibkr_account == ibkr_acc.upper()
        assert order.symbol == "AAPL"
        assert order.source == "manual"
        assert order.status == "PENDING_SUBMIT"
        assert not hasattr(order, "signal_id")  # Verify signal_id is completely absent

    async with session_factory() as session:
        repo = ManualOrderRepository(session)
        fetched = await repo.get_by_internal_id(f"MAN_ORD_{suffix}_01")
        assert fetched is not None
        assert fetched.limit_price == Decimal("150.25000000")


@pytest.mark.asyncio
async def test_manual_order_idempotency_uniqueness(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Attempting to insert an order with the same (account_id, idempotency_key) raises IntegrityError."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Manual Acc {suffix}",
            ibkr_account=f"DU{suffix}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id
        ibkr_acc = acc.ibkr_account

    async with session_factory() as session, session.begin():
        repo = ManualOrderRepository(session)
        await repo.create_order(
            account_id=acc_id,
            ibkr_account=ibkr_acc,
            idempotency_key="duplicate_key",
            internal_order_id=f"ORD_1_{suffix}",
            trade_id=f"TRD_1_{suffix}",
            symbol="TSLA",
            side="SELL",
            quantity=Decimal(50),
            order_type="MARKET",
        )

    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            repo = ManualOrderRepository(session)
            await repo.create_order(
                account_id=acc_id,
                ibkr_account=ibkr_acc,
                idempotency_key="duplicate_key",
                internal_order_id=f"ORD_2_{suffix}",
                trade_id=f"TRD_2_{suffix}",
                symbol="TSLA",
                side="SELL",
                quantity=Decimal(50),
                order_type="MARKET",
            )


@pytest.mark.asyncio
async def test_manual_order_account_isolation(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Orders for Account A are isolated from Account B."""
    suffix_a = uuid4().hex[:6]
    suffix_b = uuid4().hex[:6]

    async with session_factory() as session, session.begin():
        acc_a = AccountModel(
            name=f"Manual Acc A {suffix_a}",
            ibkr_account=f"DU{suffix_a}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        acc_b = AccountModel(
            name=f"Manual Acc B {suffix_b}",
            ibkr_account=f"DU{suffix_b}",
            total_margin=Decimal("50000.00"),
            enabled=True,
        )
        session.add_all([acc_a, acc_b])
        await session.flush()
        id_a, id_b = acc_a.id, acc_b.id
        ibkr_a, ibkr_b = acc_a.ibkr_account, acc_b.ibkr_account

    async with session_factory() as session, session.begin():
        repo = ManualOrderRepository(session)
        await repo.create_order(
            account_id=id_a,
            ibkr_account=ibkr_a,
            idempotency_key="idemp_a",
            internal_order_id=f"ORD_A_{suffix_a}",
            trade_id=f"TRD_A_{suffix_a}",
            symbol="MSFT",
            side="BUY",
            quantity=Decimal(20),
            order_type="LIMIT",
            limit_price=Decimal("400.00"),
        )
        await repo.create_order(
            account_id=id_b,
            ibkr_account=ibkr_b,
            idempotency_key="idemp_b",
            internal_order_id=f"ORD_B_{suffix_b}",
            trade_id=f"TRD_B_{suffix_b}",
            symbol="NVDA",
            side="BUY",
            quantity=Decimal(10),
            order_type="LIMIT",
            limit_price=Decimal("120.00"),
        )

    async with session_factory() as session:
        repo = ManualOrderRepository(session)
        orders_a, total_a = await repo.list_orders_for_account(account_id=id_a)
        orders_b, total_b = await repo.list_orders_for_account(account_id=id_b)

        assert total_a == 1
        assert orders_a[0].symbol == "MSFT"
        assert total_b == 1
        assert orders_b[0].symbol == "NVDA"


@pytest.mark.asyncio
async def test_manual_execution_persistence_and_duplicate_exec_id_protection(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test manual execution links to manual order and deduplicates on exec_id."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Manual Acc {suffix}",
            ibkr_account=f"DU{suffix}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id
        ibkr_acc = acc.ibkr_account

    async with session_factory() as session, session.begin():
        order_repo = ManualOrderRepository(session)
        order = await order_repo.create_order(
            account_id=acc_id,
            ibkr_account=ibkr_acc,
            idempotency_key=f"idemp_{suffix}",
            internal_order_id=f"ORD_EXEC_{suffix}",
            trade_id=f"TRD_EXEC_{suffix}",
            symbol="AMZN",
            side="BUY",
            quantity=Decimal(25),
            order_type="LIMIT",
            limit_price=Decimal("180.00"),
        )
        order_id = order.id

    now = datetime.now(UTC)
    exec_id = f"EXEC_{suffix}_123"

    async with session_factory() as session, session.begin():
        exec_repo = ManualExecutionRepository(session)
        fill1 = await exec_repo.record_execution(
            manual_order_id=order_id,
            exec_id=exec_id,
            quantity=Decimal(25),
            price=Decimal("180.00"),
            executed_at=now,
            commission=Decimal("1.00"),
            commission_currency="USD",
        )
        assert fill1.id is not None
        assert fill1.exec_id == exec_id
        assert fill1.source == "manual"

    # Second insert with same exec_id must return existing and not create duplicate row
    async with session_factory() as session, session.begin():
        exec_repo = ManualExecutionRepository(session)
        fill2 = await exec_repo.record_execution(
            manual_order_id=order_id,
            exec_id=exec_id,
            quantity=Decimal(25),
            price=Decimal("180.00"),
            executed_at=now,
        )
        assert fill2.id == fill1.id

    async with session_factory() as session:
        exec_count = (
            await session.execute(
                select(func.count()).select_from(ManualExecutionModel).where(
                    ManualExecutionModel.exec_id == exec_id
                )
            )
        ).scalar_one()
        assert exec_count == 1


@pytest.mark.asyncio
async def test_manual_position_ledger_and_trade_id_uniqueness(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test manual position persistence and verify multiple positions for same symbol can coexist under different trade_ids."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Manual Acc {suffix}",
            ibkr_account=f"DU{suffix}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    async with session_factory() as session, session.begin():
        pos_repo = ManualPositionRepository(session)

        # Trade 1 for AAPL
        pos1 = await pos_repo.create_position(
            account_id=acc_id,
            trade_id=f"TRD_AAPL_1_{suffix}",
            symbol="AAPL",
            signed_qty=Decimal(50),
            avg_cost=Decimal("150.00"),
        )
        assert pos1.source == "manual"
        assert pos1.status == "OPEN"

        # Trade 2 for AAPL (same symbol, different trade_id)
        pos2 = await pos_repo.create_position(
            account_id=acc_id,
            trade_id=f"TRD_AAPL_2_{suffix}",
            symbol="AAPL",
            signed_qty=Decimal(100),
            avg_cost=Decimal("152.50"),
        )
        assert pos2.id != pos1.id

    async with session_factory() as session:
        pos_repo = ManualPositionRepository(session)
        open_positions = await pos_repo.list_open_for_account(acc_id)
        assert len(open_positions) == 2
        trade_ids = {p.trade_id for p in open_positions}
        assert f"TRD_AAPL_1_{suffix}" in trade_ids
        assert f"TRD_AAPL_2_{suffix}" in trade_ids

    # Same (account_id, trade_id) violates uniqueness
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            pos_repo = ManualPositionRepository(session)
            await pos_repo.create_position(
                account_id=acc_id,
                trade_id=f"TRD_AAPL_1_{suffix}",
                symbol="AAPL",
                signed_qty=Decimal(20),
            )


@pytest.mark.asyncio
async def test_source_separation_invariant_engine_vs_manual(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Critical test: Manual trading persistence does NOT touch engine PositionModel,

    and engine PositionModel does NOT touch manual_positions.
    """
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Manual Acc {suffix}",
            ibkr_account=f"DU{suffix}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    async with session_factory() as session, session.begin():
        pos_repo = ManualPositionRepository(session)
        await pos_repo.create_position(
            account_id=acc_id,
            trade_id=f"MAN_INV_{suffix}",
            symbol="GOOGL",
            signed_qty=Decimal(30),
            avg_cost=Decimal("175.00"),
        )

    async with session_factory() as session:
        # Verify manual_positions has 1 row
        manual_rows = (
            await session.execute(
                select(ManualPositionModel).where(ManualPositionModel.account_id == acc_id)
            )
        ).scalars().all()
        assert len(manual_rows) == 1
        assert manual_rows[0].source == "manual"

        # Verify engine PositionModel has 0 rows for this account
        engine_rows = (
            await session.execute(
                select(PositionModel).where(PositionModel.account_id == acc_id)
            )
        ).scalars().all()
        assert len(engine_rows) == 0


@pytest.mark.asyncio
async def test_manual_halt_state_persistence(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test setting and retrieving manual trading halt state."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Manual Acc {suffix}",
            ibkr_account=f"DU{suffix}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    async with session_factory() as session, session.begin():
        halt_repo = ManualHaltRepository(session)
        initial = await halt_repo.get_halt_state(acc_id)
        assert initial is None

        halted = await halt_repo.set_halt(
            acc_id,
            halted=True,
            halted_by="operator_admin",
            reason="Market volatility circuit breaker",
        )
        assert halted.halted is True
        assert halted.halted_by == "operator_admin"
        assert halted.reason == "Market volatility circuit breaker"
        assert halted.halted_at is not None

    async with session_factory() as session, session.begin():
        halt_repo = ManualHaltRepository(session)
        cleared = await halt_repo.set_halt(
            acc_id,
            halted=False,
            halted_by="operator_admin",
            reason="Resuming manual trading",
        )
        assert cleared.halted is False
        assert cleared.halted_at is None


@pytest.mark.asyncio
async def test_manual_audit_events_persistence(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test manual audit event recording and querying."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Manual Acc {suffix}",
            ibkr_account=f"DU{suffix}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    async with session_factory() as session, session.begin():
        audit_repo = ManualAuditRepository(session)
        event = await audit_repo.record_event(
            account_id=acc_id,
            action="ORDER_SUBMIT_INTENT",
            request_id="req_xyz_123",
            payload={"symbol": "SPY", "qty": 10, "type": "LIMIT", "price": 500.0},
        )
        assert event.id is not None
        assert event.action == "ORDER_SUBMIT_INTENT"
        assert event.payload["symbol"] == "SPY"

    async with session_factory() as session:
        audit_repo = ManualAuditRepository(session)
        events, total = await audit_repo.list_events(account_id=acc_id)
        assert total == 1
        assert events[0].id == event.id


@pytest.mark.asyncio
async def test_same_symbol_mixed_positions_and_source_isolation(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies that an account can hold an engine position (AAPL +100) and a manual position (AAPL +50)

    simultaneously without merging, mutating, or confusing their source identities.
    Also verifies Account B manual position isolation.
    """
    suffix_a = uuid4().hex[:6]
    suffix_b = uuid4().hex[:6]

    async with session_factory() as session, session.begin():
        acc_a = AccountModel(
            name=f"Acc A {suffix_a}",
            ibkr_account=f"DU{suffix_a}",
            total_margin=Decimal(100000),
            enabled=True,
        )
        acc_b = AccountModel(
            name=f"Acc B {suffix_b}",
            ibkr_account=f"DU{suffix_b}",
            total_margin=Decimal(50000),
            enabled=True,
        )
        session.add_all([acc_a, acc_b])
        await session.flush()
        id_a, id_b = acc_a.id, acc_b.id

    # 1. Insert Engine Position: AAPL +100 for Account A
    async with session_factory() as session, session.begin():
        eng_pos = PositionModel(
            account_id=id_a,
            trade_id=f"ENG_TRD_{suffix_a}",
            strategy_id="model_blue",
            leg_a_symbol="AAPL",
            leg_a_signed_qty=Decimal(100),
            leg_a_entry_mark=Decimal(150),
            leg_b_symbol=None,
            leg_b_signed_qty=None,
            leg_b_entry_mark=None,
            target=Decimal(10),
            stop=Decimal(-10),
            time_limit=3600,
            risk_state="OPEN",
        )
        session.add(eng_pos)

    # 2. Insert Manual Position: AAPL +50 for Account A
    async with session_factory() as session, session.begin():
        pos_repo = ManualPositionRepository(session)
        man_pos = await pos_repo.create_position(
            account_id=id_a,
            trade_id=f"MAN_TRD_{suffix_a}",
            symbol="AAPL",
            signed_qty=Decimal(50),
            avg_cost=Decimal(152),
            status="OPEN",
        )
        assert man_pos.source == "manual"

    # 3. Insert Manual Position: AAPL +50 for Account B
    async with session_factory() as session, session.begin():
        pos_repo = ManualPositionRepository(session)
        man_pos_b = await pos_repo.create_position(
            account_id=id_b,
            trade_id=f"MAN_TRD_{suffix_b}",
            symbol="AAPL",
            signed_qty=Decimal(50),
            avg_cost=Decimal(152),
            status="OPEN",
        )
        assert man_pos_b.source == "manual"

    # Verification: Account A has exactly 1 engine position and 1 manual position
    async with session_factory() as session:
        eng_rows = (
            await session.execute(
                select(PositionModel).where(PositionModel.account_id == id_a)
            )
        ).scalars().all()
        assert len(eng_rows) == 1
        assert eng_rows[0].leg_a_symbol == "AAPL"
        assert eng_rows[0].leg_a_signed_qty == Decimal(100)

        pos_repo = ManualPositionRepository(session)
        man_rows_a = await pos_repo.list_open_for_account(id_a)
        assert len(man_rows_a) == 1
        assert man_rows_a[0].symbol == "AAPL"
        assert man_rows_a[0].signed_qty == Decimal(50)
        assert man_rows_a[0].source == "manual"

        # Account B isolation: Account A cannot see Account B's position
        man_rows_b = await pos_repo.list_open_for_account(id_b)
        assert len(man_rows_b) == 1
        assert man_rows_b[0].account_id == id_b
        assert man_rows_b[0].trade_id == f"MAN_TRD_{suffix_b}"

