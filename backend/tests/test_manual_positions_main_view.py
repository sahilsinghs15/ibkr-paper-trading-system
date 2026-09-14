"""Tests for manual positions visibility in main Positions view."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.account import AccountModel
from app.db.models.manual_order import ManualPositionModel
from app.db.models.position import PositionModel
from app.db.repositories.position_repository import RISK_STATE_OPEN
from demo_streaming.snapshot import (
    load_manual_position_rows,
    load_position_rows,
    manual_position_payload,
)


@pytest.mark.asyncio
async def test_manual_open_position_appears_in_main_positions(session_factory):
    """Single manual BUY 1 AAPL should appear in main positions view."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        # Create manual position
        mpos = ManualPositionModel(
            account_id=acc.id,
            trade_id=f"TRD-{s}",
            symbol="AAPL",
            con_id=120549942,
            sec_type="CFD",
            currency="USD",
            signed_qty=Decimal("1"),
            avg_cost=Decimal("150.00"),
            realized_pnl=Decimal("0"),
            status="OPEN",
            source="manual",
        )
        session.add(mpos)
        await session.commit()

        # Verify manual position appears via demo snapshot
        manual_rows = await load_manual_position_rows(session)
        assert any(r[0].trade_id == f"TRD-{s}" for r in manual_rows), "Manual position not found in snapshot"
        # Find our specific
        our = [r for r in manual_rows if r[0].trade_id == f"TRD-{s}"][0]
        pos, account = our
        assert pos.symbol == "AAPL"
        assert pos.signed_qty == Decimal("1")
        assert pos.status == "OPEN"
        assert account.ibkr_account == f"DU{s.upper()}"

        # Verify payload
        from datetime import datetime, timezone
        payload = manual_position_payload(pos, account, timestamp=datetime.now(timezone.utc))
        assert payload["symbol"] == "AAPL"
        assert payload["quantity"] == "1"
        assert payload["source"] == "manual"
        assert payload["status"] == "OPEN"


@pytest.mark.asyncio
async def test_engine_position_still_shown_as_engine(session_factory):
    """Engine position should still appear with source engine."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        # Engine position
        engine_pos = PositionModel(
            account_id=acc.id,
            trade_id=f"ENG-{s}",
            strategy_id="model_blue",
            leg_a_symbol="AAPL",
            leg_a_signed_qty=Decimal("10"),
            leg_a_entry_mark=Decimal("150.00"),
            leg_a_instrument_type="STK",
            leg_b_symbol="MSFT",
            leg_b_signed_qty=Decimal("-10"),
            leg_b_entry_mark=Decimal("250.00"),
            leg_b_instrument_type="STK",
            target=Decimal("0.05"),
            stop=Decimal("0.02"),
            time_limit=60,
            risk_state=RISK_STATE_OPEN,
        )
        session.add(engine_pos)
        await session.commit()

        rows = await load_position_rows(session)
        assert any(r[0].trade_id == f"ENG-{s}" for r in rows)
        # Check payload would be engine
        from demo_streaming.snapshot import position_leg_payloads
        from datetime import datetime, timezone
        for pos, acc_obj in rows:
            if pos.trade_id == f"ENG-{s}":
                payloads = position_leg_payloads(pos, acc_obj, [], [], timestamp=datetime.now(timezone.utc))
                assert any(p["source"] == "engine" for p in payloads)


@pytest.mark.asyncio
async def test_same_symbol_engine_and_manual_distinguishable(session_factory):
    """Same symbol engine + manual must remain distinguishable, not merged."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        # Engine +10 AAPL
        eng = PositionModel(
            account_id=acc.id,
            trade_id=f"ENG-{s}",
            strategy_id="model_blue",
            leg_a_symbol="AAPL",
            leg_a_signed_qty=Decimal("10"),
            leg_a_entry_mark=Decimal("100.00"),
            leg_a_instrument_type="STK",
            risk_state=RISK_STATE_OPEN,
            target=Decimal("0.05"),
            stop=Decimal("0.02"),
            time_limit=60,
        )
        session.add(eng)
        # Manual +1 AAPL
        man = ManualPositionModel(
            account_id=acc.id,
            trade_id=f"MAN-{s}",
            symbol="AAPL",
            con_id=120549942,
            sec_type="CFD",
            signed_qty=Decimal("1"),
            avg_cost=Decimal("150.00"),
            status="OPEN",
            source="manual",
        )
        session.add(man)
        await session.commit()

        engine_rows = await load_position_rows(session)
        manual_rows = await load_manual_position_rows(session)
        # Both exist
        assert any(r[0].trade_id == f"ENG-{s}" for r in engine_rows)
        assert any(r[0].trade_id == f"MAN-{s}" for r in manual_rows)
        # Account isolation would still hold, but here same account, verify not merged
        # They are separate rows, not merged
        all_symbols = [r[0].symbol if hasattr(r[0], "symbol") else r[0].leg_a_symbol for r in manual_rows + engine_rows]  # type: ignore
        assert "AAPL" in all_symbols


@pytest.mark.asyncio
async def test_closed_manual_not_in_open_view(session_factory):
    """Closed manual position should NOT appear in open positions."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"Acc-{s}", ibkr_account=f"DU{s.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        man_closed = ManualPositionModel(
            account_id=acc.id,
            trade_id=f"TRD-CLOSED-{s}",
            symbol="AAPL",
            con_id=120549942,
            sec_type="CFD",
            signed_qty=Decimal("0"),
            avg_cost=Decimal("0"),
            status="CLOSED",
            source="manual",
        )
        session.add(man_closed)
        await session.commit()

        manual_rows = await load_manual_position_rows(session)
        assert not any(r[0].trade_id == f"TRD-CLOSED-{s}" for r in manual_rows), "Closed manual should not be in OPEN view"


@pytest.mark.asyncio
async def test_account_isolation_manual_positions(session_factory):
    """Manual positions must be isolated per account."""
    s1 = uuid.uuid4().hex[:4]
    s2 = uuid.uuid4().hex[:4]
    async with session_factory() as session:
        acc1 = AccountModel(name=f"Acc1-{s1}", ibkr_account=f"DU{s1.upper()}", total_margin=Decimal("100000"), enabled=True)
        acc2 = AccountModel(name=f"Acc2-{s2}", ibkr_account=f"DU{s2.upper()}", total_margin=Decimal("100000"), enabled=True)
        session.add_all([acc1, acc2])
        await session.commit()
        await session.refresh(acc1)
        await session.refresh(acc2)
        m1 = ManualPositionModel(account_id=acc1.id, trade_id=f"TRD-{s1}", symbol="AAPL", con_id=120549942, sec_type="CFD", signed_qty=Decimal("1"), avg_cost=Decimal("100"), status="OPEN", source="manual")
        m2 = ManualPositionModel(account_id=acc2.id, trade_id=f"TRD-{s2}", symbol="AAPL", con_id=120549942, sec_type="CFD", signed_qty=Decimal("5"), avg_cost=Decimal("100"), status="OPEN", source="manual")
        session.add_all([m1, m2])
        await session.commit()

        manual_rows = await load_manual_position_rows(session)
        acc1_rows = [r for r in manual_rows if r[0].account_id == acc1.id]
        acc2_rows = [r for r in manual_rows if r[0].account_id == acc2.id]
        assert len(acc1_rows) == 1 and acc1_rows[0][0].signed_qty == Decimal("1")
        assert len(acc2_rows) == 1 and acc2_rows[0][0].signed_qty == Decimal("5")
        # No leakage
        assert acc1_rows[0][0].trade_id != acc2_rows[0][0].trade_id
