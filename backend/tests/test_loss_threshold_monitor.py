"""Focused tests for realized P&L loss threshold notification."""

import asyncio
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models.account import AccountModel
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.rms.models import OrderSide


def _leg(symbol, side, qty, price):
    return OpenModelBlueTradeLeg(symbol=symbol, side=side, quantity=qty, price=price, instrument_type="STK")


@pytest.mark.asyncio
async def test_threshold_disabled_no_alert(session_factory):
    suffix = uuid.uuid4().hex[:6]
    from app.services.loss_threshold_monitor import LossThresholdMonitor
    async with session_factory() as session:
        acc = AccountModel(name=f"LT{suffix}", ibkr_account=f"DULT{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=None)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        mon = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)) as mock:
            res = await mon.evaluate_account(acc.id)
            assert res is None
            mock.assert_not_called()


@pytest.mark.asyncio
async def test_above_threshold_no_alert(session_factory):
    suffix = uuid.uuid4().hex[:6]
    from app.services.loss_threshold_monitor import LossThresholdMonitor
    async with session_factory() as session:
        acc = AccountModel(name=f"LT{suffix}", ibkr_account=f"DULT{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=Decimal(-500))
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        # realized -200 (above -500)
        # create closed position with -200
        trade = OpenModelBlueTrade(trade_id=f"T{suffix}", strategy_id="model_blue", direction=0, legs=(_leg("AAPL", OrderSide.BUY, Decimal(1), Decimal(100)), _leg("MSFT", OrderSide.SELL, Decimal(1), Decimal(100))))
        from app.db.repositories.position_repository import PositionRepository
        repo = PositionRepository(session)
        row = await repo.open_trade(trade, account_id=acc.id, target=Decimal(500), stop=Decimal(-250), time_limit=3600)
        row.realised_pnl = Decimal(-200)
        row.risk_state = "CLOSED"
        from datetime import UTC, datetime
        row.closed_at = datetime.now(UTC)
        await session.commit()
        mon = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)) as mock:
            res = await mon.evaluate_account(acc.id)
            assert res is None
            mock.assert_not_called()


@pytest.mark.asyncio
async def test_exact_at_threshold_alert(session_factory):
    suffix = uuid.uuid4().hex[:6]
    from app.services.loss_threshold_monitor import LossThresholdMonitor
    async with session_factory() as session:
        acc = AccountModel(name=f"LT{suffix}", ibkr_account=f"DULT{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=Decimal(-500))
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        trade = OpenModelBlueTrade(trade_id=f"T{suffix}", strategy_id="model_blue", direction=0, legs=(_leg("AAPL", OrderSide.BUY, Decimal(1), Decimal(100)), _leg("MSFT", OrderSide.SELL, Decimal(1), Decimal(100))))
        from app.db.repositories.position_repository import PositionRepository
        repo = PositionRepository(session)
        row = await repo.open_trade(trade, account_id=acc.id, target=Decimal(500), stop=Decimal(-250), time_limit=3600)
        row.realised_pnl = Decimal(-500)
        row.risk_state = "CLOSED"
        from datetime import UTC, datetime
        row.closed_at = datetime.now(UTC)
        await session.commit()
        mon = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)):
            res = await mon.evaluate_account(acc.id)
            assert res is not None
            # let telegram task run
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_repeated_while_breached_only_one(session_factory):
    suffix = uuid.uuid4().hex[:6]
    from app.services.loss_threshold_monitor import LossThresholdMonitor
    async with session_factory() as session:
        acc = AccountModel(name=f"LT{suffix}", ibkr_account=f"DULT{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=Decimal(-500))
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        trade = OpenModelBlueTrade(trade_id=f"T{suffix}", strategy_id="model_blue", direction=0, legs=(_leg("AAPL", OrderSide.BUY, Decimal(1), Decimal(100)), _leg("MSFT", OrderSide.SELL, Decimal(1), Decimal(100))))
        from sqlalchemy import select

        from app.db.models.event import EventLogModel
        from app.db.repositories.position_repository import PositionRepository
        repo = PositionRepository(session)
        row = await repo.open_trade(trade, account_id=acc.id, target=Decimal(500), stop=Decimal(-250), time_limit=3600)
        row.realised_pnl = Decimal(-600)
        row.risk_state = "CLOSED"
        from datetime import UTC, datetime
        row.closed_at = datetime.now(UTC)
        await session.commit()
        mon = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)):
            await mon.evaluate_account(acc.id)
            await asyncio.sleep(0.05)
            await mon.evaluate_account(acc.id)
            await asyncio.sleep(0.05)
            await mon.evaluate_account(acc.id)
            await asyncio.sleep(0.05)
        events = list((await session.execute(select(EventLogModel).where(EventLogModel.kind == "LOSS_THRESHOLD_BREACHED"))).scalars().all())
        # Filter to this account
        mine = [e for e in events if str(e.detail.get("account_id")) == str(acc.id)]
        assert len(mine) == 1


@pytest.mark.asyncio
async def test_recovery_resets_and_second_crossing(session_factory):
    suffix = uuid.uuid4().hex[:6]
    from app.services.loss_threshold_monitor import LossThresholdMonitor
    async with session_factory() as session:
        acc = AccountModel(name=f"LT{suffix}", ibkr_account=f"DULT{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=Decimal(-500))
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        trade = OpenModelBlueTrade(trade_id=f"T{suffix}", strategy_id="model_blue", direction=0, legs=(_leg("AAPL", OrderSide.BUY, Decimal(1), Decimal(100)), _leg("MSFT", OrderSide.SELL, Decimal(1), Decimal(100))))
        from app.db.repositories.position_repository import PositionRepository
        repo = PositionRepository(session)
        row = await repo.open_trade(trade, account_id=acc.id, target=Decimal(500), stop=Decimal(-250), time_limit=3600)
        row.realised_pnl = Decimal(-600)
        row.risk_state = "CLOSED"
        from datetime import UTC, datetime
        row.closed_at = datetime.now(UTC)
        await session.commit()
        mon = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)):
            await mon.evaluate_account(acc.id)
            await asyncio.sleep(0.05)
            # recover to -400 by updating realised
            row.realised_pnl = Decimal(-400)
            await session.commit()
            await mon.evaluate_account(acc.id)
            await asyncio.sleep(0.05)
            # second crossing to -501
            row.realised_pnl = Decimal(-501)
            await session.commit()
            res = await mon.evaluate_account(acc.id)
            await asyncio.sleep(0.05)
            assert res is not None
        from sqlalchemy import select

        from app.db.models.event import EventLogModel
        events = list((await session.execute(select(EventLogModel).where(EventLogModel.kind == "LOSS_THRESHOLD_BREACHED"))).scalars().all())
        mine = [e for e in events if str(e.detail.get("account_id")) == str(acc.id)]
        assert len(mine) == 2


@pytest.mark.asyncio
async def test_restart_while_breached_no_duplicate(session_factory):
    suffix = uuid.uuid4().hex[:6]
    from app.services.loss_threshold_monitor import LossThresholdMonitor
    async with session_factory() as session:
        acc = AccountModel(name=f"LT{suffix}", ibkr_account=f"DULT{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=Decimal(-500))
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        trade = OpenModelBlueTrade(trade_id=f"T{suffix}", strategy_id="model_blue", direction=0, legs=(_leg("AAPL", OrderSide.BUY, Decimal(1), Decimal(100)), _leg("MSFT", OrderSide.SELL, Decimal(1), Decimal(100))))
        from app.db.repositories.position_repository import PositionRepository
        repo = PositionRepository(session)
        row = await repo.open_trade(trade, account_id=acc.id, target=Decimal(500), stop=Decimal(-250), time_limit=3600)
        row.realised_pnl = Decimal(-600)
        row.risk_state = "CLOSED"
        from datetime import UTC, datetime
        row.closed_at = datetime.now(UTC)
        await session.commit()
        mon = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)):
            await mon.evaluate_account(acc.id)
            await asyncio.sleep(0.05)
        # simulate restart: new monitor instance, same DB state is_below=true
        mon2 = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)) as mock2:
            res = await mon2.evaluate_account(acc.id)
            assert res is None
            mock2.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_no_duplicate(session_factory):
    suffix = uuid.uuid4().hex[:6]
    from app.services.loss_threshold_monitor import LossThresholdMonitor
    async with session_factory() as session:
        acc = AccountModel(name=f"LT{suffix}", ibkr_account=f"DULT{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=Decimal(-500))
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        trade = OpenModelBlueTrade(trade_id=f"T{suffix}", strategy_id="model_blue", direction=0, legs=(_leg("AAPL", OrderSide.BUY, Decimal(1), Decimal(100)), _leg("MSFT", OrderSide.SELL, Decimal(1), Decimal(100))))
        from app.db.repositories.position_repository import PositionRepository
        repo = PositionRepository(session)
        row = await repo.open_trade(trade, account_id=acc.id, target=Decimal(500), stop=Decimal(-250), time_limit=3600)
        row.realised_pnl = Decimal(-600)
        row.risk_state = "CLOSED"
        from datetime import UTC, datetime
        row.closed_at = datetime.now(UTC)
        await session.commit()
        mon = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)):
            await asyncio.gather(*(mon.evaluate_account(acc.id) for _ in range(5)))
            await asyncio.sleep(0.1)
        from sqlalchemy import select

        from app.db.models.event import EventLogModel
        events = list((await session.execute(select(EventLogModel).where(EventLogModel.kind == "LOSS_THRESHOLD_BREACHED"))).scalars().all())
        mine = [e for e in events if str(e.detail.get("account_id")) == str(acc.id)]
        assert len(mine) == 1


@pytest.mark.asyncio
async def test_account_isolation(session_factory):
    suffix = uuid.uuid4().hex[:6]
    from app.services.loss_threshold_monitor import LossThresholdMonitor
    async with session_factory() as session:
        a = AccountModel(name=f"A{suffix}", ibkr_account=f"DUA{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=Decimal(-500))
        b = AccountModel(name=f"B{suffix}", ibkr_account=f"DUB{suffix.upper()}", total_margin=10000, enabled=True, loss_threshold=Decimal(-1000))
        session.add_all([a, b])
        await session.commit()
        await session.refresh(a)
        await session.refresh(b)
        # A has -600 (breach), B has -600 (not breach for -1000)
        for acc, val in [(a, Decimal(-600)), (b, Decimal(-600))]:
            trade = OpenModelBlueTrade(trade_id=f"T{acc.id}{suffix}", strategy_id="model_blue", direction=0, legs=(_leg("AAPL", OrderSide.BUY, Decimal(1), Decimal(100)), _leg("MSFT", OrderSide.SELL, Decimal(1), Decimal(100))))
            from app.db.repositories.position_repository import PositionRepository
            repo = PositionRepository(session)
            row = await repo.open_trade(trade, account_id=acc.id, target=Decimal(500), stop=Decimal(-250), time_limit=3600)
            row.realised_pnl = val
            row.risk_state = "CLOSED"
            from datetime import UTC, datetime
            row.closed_at = datetime.now(UTC)
        await session.commit()
        mon = LossThresholdMonitor(session_factory)
        with patch("app.services.loss_threshold_monitor.send_canonical_telegram", new=AsyncMock(return_value=True)):
            await mon.evaluate_account(a.id)
            await mon.evaluate_account(b.id)
            await asyncio.sleep(0.1)
        from sqlalchemy import select

        from app.db.models.event import EventLogModel
        events = list((await session.execute(select(EventLogModel).where(EventLogModel.kind == "LOSS_THRESHOLD_BREACHED"))).scalars().all())
        mine_a = [e for e in events if str(e.detail.get("account_id")) == str(a.id)]
        mine_b = [e for e in events if str(e.detail.get("account_id")) == str(b.id)]
        assert len(mine_a) == 1
        assert len(mine_b) == 0
