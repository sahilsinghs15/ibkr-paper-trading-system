"""Unit tests for Phase 2 persistent PostgreSQL database schema."""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db.models import (
    AccountModel,
    AllocationModel,
    BasketModel,
    EventLogModel,
    ExecutionModel,
    InstrumentModel,
    OrderModel,
    PerSymbolLimitModel,
    PositionModel,
    SignalModel,
    StrategyModel,
)
from app.db.session import create_engine_from_settings


def test_schema_metadata_tables() -> None:
    """Verify that all required domain tables are registered on Base.metadata."""
    table_names = set(Base.metadata.tables.keys())
    expected_tables = {
        "signals",
        "accounts",
        "strategies",
        "allocations",
        "per_symbol_limits",
        "orders",
        "event_log",
        "positions",
            "baskets",
            "instruments",
            "executions",
        }
    assert expected_tables.issubset(table_names)


def test_signals_table_constraints() -> None:
    """Verify unique constraint on signals (strategy_id, signal_id)."""
    table = Base.metadata.tables["signals"]
    unique_cols = [
        set(c.columns.keys())
        for c in table.constraints
        if hasattr(c, "columns")
    ]
    assert {"strategy_id", "signal_id"} in unique_cols


def test_allocations_unique_account_strategy() -> None:
    table = Base.metadata.tables["allocations"]
    unique_cols = [
        set(c.columns.keys())
        for c in table.constraints
        if hasattr(c, "columns")
    ]
    assert {"account_id", "strategy_id"} in unique_cols


def test_baskets_unique_account_trade_action() -> None:
    table = Base.metadata.tables["baskets"]
    unique_cols = [
        set(c.columns.keys())
        for c in table.constraints
        if hasattr(c, "columns")
    ]
    assert {"account_id", "trade_id", "action"} in unique_cols


def test_orders_table_indexes() -> None:
    """Verify indexes on orders table."""
    table = Base.metadata.tables["orders"]
    index_names = {idx.name for idx in table.indexes}
    assert "ix_orders_account_status" in index_names


@pytest.mark.asyncio
async def test_schema_crud_operations() -> None:
    """Test CRUD operations for domain models against Alembic-migrated PostgreSQL schema."""
    test_engine = create_engine_from_settings()
    session_factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )

    try:
        async with session_factory() as session:
            # 1. Create Account
            acct = AccountModel(
                name="Test Account 1",
                ibkr_account="DU123456",
                total_margin=Decimal("100000.00"),
                enabled=True,
            )
            session.add(acct)
            await session.commit()
            await session.refresh(acct)
            assert acct.id is not None

            # 2. Create Strategy
            strat = StrategyModel(
                strategy_id="MODEL_BLUE",
                legs=2,
                expression="CFD",
                max_open_positions=10,
                weight_source="payload",
                target_delta=None,
                enabled=True,
            )
            session.add(strat)
            await session.commit()
            await session.refresh(strat)
            assert strat.id is not None

            # 3. Create Allocation
            alloc = AllocationModel(
                account_id=acct.id,
                strategy_id=strat.strategy_id,
                alloc_pct=Decimal("0.50"),
                target=Decimal("500.00"),
                stop=Decimal("250.00"),
                time_limit=3600,
                max_open_positions=10,
            )
            session.add(alloc)
            await session.commit()

            # 4. Create Signal
            sig = SignalModel(
                strategy_id="MODEL_BLUE",
                signal_id="SIG_1001",
                action="OPEN",
                pair="EWA:EWC",
                side="LONG_A_SHORT_B",
                ref_price_a=Decimal("25.50"),
                ref_price_b=Decimal("30.10"),
                raw_payload={"test": "payload"},
                status="NEW",
            )
            session.add(sig)
            await session.commit()
            await session.refresh(sig)
            assert sig.id is not None

            # 5. Create Order
            order = OrderModel(
                signal_id=sig.id,
                account_id=acct.id,
                strategy_id="MODEL_BLUE",
                leg="A",
                symbol="EWA",
                ibkr_contract="EWA-STK-SMART-USD",
                buy_sell="BUY",
                quantity=100,
                limit_price=Decimal("25.50"),
                status="PENDING",
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            assert order.id is not None

            # 6. Create Event Log
            evt = EventLogModel(
                process="strategy",
                signal_id=sig.id,
                order_id=order.id,
                kind="RMS_PASS",
                detail={"check": 1, "passed": True},
            )
            session.add(evt)
            await session.commit()
            assert evt.id is not None

            # 7. Create Position
            pos = PositionModel(
                trade_id="TRD_9999",
                strategy_id="MODEL_BLUE",
                account_id=acct.id,
                leg_a_symbol="EWA",
                leg_a_signed_qty=100,
                leg_a_entry_mark=Decimal("25.50"),
                target=Decimal("500.00"),
                stop=Decimal("250.00"),
                time_limit=3600,
                risk_state="OPEN",
            )
            session.add(pos)
            await session.commit()
            assert pos.trade_id == "TRD_9999"

            # 8. Create Instrument
            inst = InstrumentModel(
                symbol="EWA",
                sec_type="CFD",
                trade_conid=123456,
                market_data_conid=654321,
                underlying_exchange="ARCA",
                exchange="SMART",
                currency="USD",
                multiplier=Decimal("1.0"),
            )
            session.add(inst)
            await session.commit()
            assert inst.symbol == "EWA"

            # Clean up test rows
            await session.delete(inst)
            await session.delete(pos)
            await session.delete(evt)
            await session.delete(order)
            await session.delete(sig)
            await session.delete(alloc)
            await session.delete(strat)
            await session.delete(acct)
            await session.commit()

    finally:
        await test_engine.dispose()
