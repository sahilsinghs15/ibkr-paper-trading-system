"""Unit and integration tests for repair_historical_killswitch_positions.py script."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.order import OrderModel
from app.db.models.signal import SignalModel
from app.db.repositories.position_repository import (
    RISK_STATE_CLOSED,
    RISK_STATE_OPEN,
    PositionRepository,
)
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.rms.models import OrderSide
from scripts.repair_historical_killswitch_positions import audit_and_repair_positions


@pytest.fixture
async def session_factory():
    from app.db.session import AsyncSessionLocal, engine
    yield AsyncSessionLocal
    await engine.dispose()


@pytest.mark.asyncio
async def test_repair_dry_run_zero_writes(session_factory: async_sessionmaker[AsyncSession]):
    """Verify dry-run mode performs zero DB writes."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-DRY-RUN-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="DryRunAcc", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        sig = SignalModel(
            signal_id=f"SIG-{test_id}", strategy_id="model_blue", action="CLOSE", pair="AAPL-MSFT", side="SELL",
            ref_price_a=Decimal("150.0"), ref_price_b=Decimal("300.0"), status="ACCEPTED", raw_payload={},
        )
        session.add(sig)
        await session.flush()

        trade = OpenModelBlueTrade(
            trade_id=trade_id, strategy_id="model_blue", direction=1,
            legs=(
                OpenModelBlueTradeLeg(symbol="AAPL", instrument_type="STK", side=OrderSide.BUY, quantity=Decimal("100.00"), price=Decimal("150.00")),
                OpenModelBlueTradeLeg(symbol="MSFT", instrument_type="STK", side=OrderSide.SELL, quantity=Decimal("50.00"), price=Decimal("300.00")),
            ),
        )
        await PositionRepository(session).open_trade(trade, account_id=acc_id, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60)

        o1 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L0", symbol="AAPL", ibkr_contract="STK", buy_sell="SELL", quantity=Decimal("100.00"), limit_price=Decimal("155.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L0", fill_price=Decimal("155.00"), fill_qty=Decimal("100.00"))
        o2 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L1", symbol="MSFT", ibkr_contract="STK", buy_sell="BUY", quantity=Decimal("50.00"), limit_price=Decimal("295.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L1", fill_price=Decimal("295.00"), fill_qty=Decimal("50.00"))
        session.add_all([o1, o2])

    res = await audit_and_repair_positions(session_factory, account_id=acc_id, apply_changes=False)
    assert res["apply_mode"] is False
    assert res["eligible_count"] == 1
    assert res["db_writes"] == 0

    # Verify DB position is STILL OPEN
    async with session_factory() as session:
        pos = await PositionRepository(session).get_by_trade_id(trade_id, account_id=acc_id)
        assert pos is not None
        assert pos.risk_state == RISK_STATE_OPEN
        assert pos.closed_at is None


@pytest.mark.asyncio
async def test_repair_apply_mode_closes_position(session_factory: async_sessionmaker[AsyncSession]):
    """Verify apply mode closes eligible position and sets closed_at."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-APPLY-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="ApplyAcc", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        sig = SignalModel(
            signal_id=f"SIG-{test_id}", strategy_id="model_blue", action="CLOSE", pair="EWA-EZU", side="SELL",
            ref_price_a=Decimal("30.0"), ref_price_b=Decimal("70.0"), status="ACCEPTED", raw_payload={},
        )
        session.add(sig)
        await session.flush()

        trade = OpenModelBlueTrade(
            trade_id=trade_id, strategy_id="model_blue", direction=1,
            legs=(
                OpenModelBlueTradeLeg(symbol="EWA", instrument_type="STK", side=OrderSide.BUY, quantity=Decimal("840.00"), price=Decimal("29.00")),
                OpenModelBlueTradeLeg(symbol="EZU", instrument_type="STK", side=OrderSide.SELL, quantity=Decimal("279.00"), price=Decimal("70.00")),
            ),
        )
        await PositionRepository(session).open_trade(trade, account_id=acc_id, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60)

        o1 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L0", symbol="EWA", ibkr_contract="STK", buy_sell="SELL", quantity=Decimal("840.00"), limit_price=Decimal("30.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L0", fill_price=Decimal("30.00"), fill_qty=Decimal("840.00"))
        o2 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L1", symbol="EZU", ibkr_contract="STK", buy_sell="BUY", quantity=Decimal("279.00"), limit_price=Decimal("71.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L1", fill_price=Decimal("71.00"), fill_qty=Decimal("279.00"))
        session.add_all([o1, o2])

    res = await audit_and_repair_positions(session_factory, account_id=acc_id, apply_changes=True)
    assert res["apply_mode"] is True
    assert res["eligible_count"] == 1
    assert res["db_writes"] == 1

    # Verify DB position is CLOSED with closed_at set
    async with session_factory() as session:
        pos = await PositionRepository(session).get_by_trade_id(trade_id, account_id=acc_id)
        assert pos is not None
        assert pos.risk_state == RISK_STATE_CLOSED
        assert pos.closed_at is not None


@pytest.mark.asyncio
async def test_repair_partial_fill_rejected(session_factory: async_sessionmaker[AsyncSession]):
    """Verify partially filled close orders are NOT repaired."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-PARTIAL-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="PartialAcc", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        sig = SignalModel(
            signal_id=f"SIG-{test_id}", strategy_id="model_blue", action="CLOSE", pair="EWC-EWG", side="SELL",
            ref_price_a=Decimal("60.0"), ref_price_b=Decimal("40.0"), status="ACCEPTED", raw_payload={},
        )
        session.add(sig)
        await session.flush()

        trade = OpenModelBlueTrade(
            trade_id=trade_id, strategy_id="model_blue", direction=1,
            legs=(
                OpenModelBlueTradeLeg(symbol="EWC", instrument_type="STK", side=OrderSide.BUY, quantity=Decimal("400.00"), price=Decimal("60.00")),
                OpenModelBlueTradeLeg(symbol="EWG", instrument_type="STK", side=OrderSide.SELL, quantity=Decimal("600.00"), price=Decimal("40.00")),
            ),
        )
        await PositionRepository(session).open_trade(trade, account_id=acc_id, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60)

        # Only 200 of 400 EWC filled
        o1 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L0", symbol="EWC", ibkr_contract="STK", buy_sell="SELL", quantity=Decimal("400.00"), limit_price=Decimal("60.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L0", fill_price=Decimal("60.00"), fill_qty=Decimal("200.00"))
        o2 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L1", symbol="EWG", ibkr_contract="STK", buy_sell="BUY", quantity=Decimal("600.00"), limit_price=Decimal("40.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L1", fill_price=Decimal("40.00"), fill_qty=Decimal("600.00"))
        session.add_all([o1, o2])

    res = await audit_and_repair_positions(session_factory, account_id=acc_id, apply_changes=True)
    assert res["eligible_count"] == 0
    assert res["rejected_count"] == 1
    assert res["db_writes"] == 0


@pytest.mark.asyncio
async def test_repair_multiple_retry_fragments_accumulate(session_factory: async_sessionmaker[AsyncSession]):
    """Verify multiple close order retry fragments correctly accumulate to 100% fill."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-FRAG-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="FragAcc", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        sig = SignalModel(
            signal_id=f"SIG-{test_id}", strategy_id="model_blue", action="CLOSE", pair="SIL-GDX", side="SELL",
            ref_price_a=Decimal("90.0"), ref_price_b=Decimal("90.0"), status="ACCEPTED", raw_payload={},
        )
        session.add(sig)
        await session.flush()

        trade = OpenModelBlueTrade(
            trade_id=trade_id, strategy_id="model_blue", direction=1,
            legs=(
                OpenModelBlueTradeLeg(symbol="SIL", instrument_type="STK", side=OrderSide.BUY, quantity=Decimal("100.00"), price=Decimal("90.00")),
                OpenModelBlueTradeLeg(symbol="GDX", instrument_type="STK", side=OrderSide.SELL, quantity=Decimal("50.00"), price=Decimal("90.00")),
            ),
        )
        await PositionRepository(session).open_trade(trade, account_id=acc_id, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60)

        # 2 fragment orders for SIL (60 + 40 = 100)
        o1a = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L0", symbol="SIL", ibkr_contract="STK", buy_sell="SELL", quantity=Decimal("60.00"), limit_price=Decimal("90.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L0-frag1", fill_price=Decimal("90.00"), fill_qty=Decimal("60.00"))
        o1b = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L0", symbol="SIL", ibkr_contract="STK", buy_sell="SELL", quantity=Decimal("40.00"), limit_price=Decimal("90.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L0-frag2", fill_price=Decimal("90.00"), fill_qty=Decimal("40.00"))
        o2 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L1", symbol="GDX", ibkr_contract="STK", buy_sell="BUY", quantity=Decimal("50.00"), limit_price=Decimal("90.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L1", fill_price=Decimal("90.00"), fill_qty=Decimal("50.00"))
        session.add_all([o1a, o1b, o2])

    res = await audit_and_repair_positions(session_factory, account_id=acc_id, apply_changes=True)
    assert res["eligible_count"] == 1
    assert res["db_writes"] == 1


@pytest.mark.asyncio
async def test_repair_idempotency(session_factory: async_sessionmaker[AsyncSession]):
    """Verify script is idempotent when executed a second time."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-IDEM-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="IdemAcc", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        sig = SignalModel(
            signal_id=f"SIG-{test_id}", strategy_id="model_blue", action="CLOSE", pair="XLF-XME", side="SELL",
            ref_price_a=Decimal("50.0"), ref_price_b=Decimal("100.0"), status="ACCEPTED", raw_payload={},
        )
        session.add(sig)
        await session.flush()

        trade = OpenModelBlueTrade(
            trade_id=trade_id, strategy_id="model_blue", direction=1,
            legs=(
                OpenModelBlueTradeLeg(symbol="XLF", instrument_type="STK", side=OrderSide.BUY, quantity=Decimal("100.00"), price=Decimal("50.00")),
                OpenModelBlueTradeLeg(symbol="XME", instrument_type="STK", side=OrderSide.SELL, quantity=Decimal("50.00"), price=Decimal("100.00")),
            ),
        )
        await PositionRepository(session).open_trade(trade, account_id=acc_id, target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60)

        o1 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L0", symbol="XLF", ibkr_contract="STK", buy_sell="SELL", quantity=Decimal("100.00"), limit_price=Decimal("50.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L0", fill_price=Decimal("50.00"), fill_qty=Decimal("100.00"))
        o2 = OrderModel(signal_id=sig.id, account_id=acc_id, strategy_id="model_blue", leg="L1", symbol="XME", ibkr_contract="STK", buy_sell="BUY", quantity=Decimal("50.00"), limit_price=Decimal("100.00"), status="FILLED", trade_id=trade_id, internal_order_id=f"KILLSWITCH-{trade_id}-L1", fill_price=Decimal("100.00"), fill_qty=Decimal("50.00"))
        session.add_all([o1, o2])

    # Run 1: Apply repair
    res1 = await audit_and_repair_positions(session_factory, account_id=acc_id, apply_changes=True)
    assert res1["eligible_count"] == 1
    assert res1["db_writes"] == 1

    # Run 2: Second run should find 0 open positions to repair
    res2 = await audit_and_repair_positions(session_factory, account_id=acc_id, apply_changes=True)
    assert res2["eligible_count"] == 0
    assert res2["db_writes"] == 0
