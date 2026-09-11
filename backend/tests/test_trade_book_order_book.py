"""Focused tests for Trade Book persistence + Order Book."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.broker.ibkr.executions import BrokerExecutionLine
from app.db.models.account import AccountModel
from app.db.models.order import OrderModel
from app.db.models.signal import SignalModel
from app.db.models.trade_execution import TradeExecutionModel
from app.db.repositories.trade_execution_repository import TradeExecutionRepository


def _line(exec_id="exec.1", symbol="AAPL", qty=10.0, price=150.0, acct="DUA123", broker_order_id=1001):
    return BrokerExecutionLine(
        exec_id=exec_id,
        executed_at="2026-09-10T10:00:00+00:00",
        ibkr_account=acct,
        symbol=symbol,
        sec_type="STK",
        currency="USD",
        exchange="SMART",
        con_id=111,
        side="BUY",
        quantity=qty,
        price=price,
        cum_qty=qty,
        avg_price=price,
        broker_order_id=broker_order_id,
        perm_id=999,
        client_id=0,
        commission=1.0,
        commission_currency="USD",
        realized_pnl=5.0,
    )


@pytest.mark.asyncio
async def test_trade_book_upsert_idempotent(session_factory) -> None:
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"TA{suffix}", ibkr_account=f"DUT{suffix.upper()}", total_margin=10000, enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        eid = f"exec.idem.{suffix}"
        repo = TradeExecutionRepository(session)
        await repo.upsert_one(_line(eid, acct=acc.ibkr_account), account_id=acc.id)
        await session.commit()
        await repo.upsert_one(_line(eid, acct=acc.ibkr_account), account_id=acc.id)
        await session.commit()
        rows = (await session.execute(select(TradeExecutionModel).where(TradeExecutionModel.exec_id == eid))).scalars().all()
        assert len(rows) == 1
        # correction - same exec_id updated price
        await repo.upsert_one(_line(eid, acct=acc.ibkr_account, price=151.0), account_id=acc.id)
        await session.commit()
        row = (await session.execute(select(TradeExecutionModel).where(TradeExecutionModel.exec_id == eid))).scalar_one()
        assert float(row.price) == 151.0
        assert row.correction_count == 1


@pytest.mark.asyncio
async def test_trade_book_repeated_polling_no_duplicates(session_factory) -> None:
    """N executions received repeatedly = N unique rows"""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"RP{suffix}", ibkr_account=f"DURP{suffix.upper()}", total_margin=10000, enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        repo = TradeExecutionRepository(session)
        ids = [f"exec.rp.{suffix}.{i}" for i in range(3)]
        batch = [_line(eid, acct=acc.ibkr_account, broker_order_id=3000+i) for i, eid in enumerate(ids)]
        # Poll 1: A
        await repo.upsert_batch(batch[:1], account_id=acc.id)
        await session.commit()
        # Poll 2: A+B
        await repo.upsert_batch(batch[:2], account_id=acc.id)
        await session.commit()
        # Poll 3: A+B+C
        await repo.upsert_batch(batch, account_id=acc.id)
        await session.commit()
        # Poll 4+5: repeat full batch
        await repo.upsert_batch(batch, account_id=acc.id)
        await session.commit()
        await repo.upsert_batch(batch, account_id=acc.id)
        await session.commit()
        count = await repo.count_for_account(acc.id)
        assert count == 3


@pytest.mark.asyncio
async def test_trade_book_backend_restart_resync(session_factory) -> None:
    """Restart must not duplicate"""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"RS{suffix}", ibkr_account=f"DURS{suffix.upper()}", total_margin=10000, enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        repo = TradeExecutionRepository(session)
        eid = f"exec.restart.{suffix}"
        await repo.upsert_one(_line(eid, acct=acc.ibkr_account), account_id=acc.id)
        await session.commit()
    # Simulate restart: new session_factory (same DB) fetch same + new
    async with session_factory() as session2:
        repo2 = TradeExecutionRepository(session2)
        await repo2.upsert_one(_line(eid, acct=acc.ibkr_account), account_id=acc.id)
        await session2.commit()
        count = await repo2.count_for_account(acc.id)
        assert count == 1


@pytest.mark.asyncio
async def test_trade_book_account_isolation(session_factory) -> None:
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        a = AccountModel(name=f"A{suffix}", ibkr_account=f"DUA{suffix.upper()}", total_margin=10000, enabled=True)
        b = AccountModel(name=f"B{suffix}", ibkr_account=f"DUB{suffix.upper()}", total_margin=10000, enabled=True)
        session.add_all([a, b])
        await session.commit()
        await session.refresh(a)
        await session.refresh(b)
        repo = TradeExecutionRepository(session)
        await repo.upsert_one(_line("exec.isoA", acct=a.ibkr_account), account_id=a.id)
        await repo.upsert_one(_line("exec.isoB", acct=b.ibkr_account), account_id=b.id)
        await session.commit()
        ra, total_a = await repo.list_paginated(account_id=a.id, page=1, page_size=10)
        rb, total_b = await repo.list_paginated(account_id=b.id, page=1, page_size=10)
        assert total_a == 1 and ra[0].exec_id == "exec.isoA"
        assert total_b == 1 and rb[0].exec_id == "exec.isoB"


@pytest.mark.asyncio
async def test_trade_book_pagination(session_factory) -> None:
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"P{suffix}", ibkr_account=f"DUP{suffix.upper()}", total_margin=10000, enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        repo = TradeExecutionRepository(session)
        for i in range(5):
            await repo.upsert_one(_line(f"exec.page.{suffix}.{i}", acct=acc.ibkr_account, broker_order_id=2000+i), account_id=acc.id)
        await session.commit()
        rows1, total = await repo.list_paginated(account_id=acc.id, page=1, page_size=2)
        rows2, _ = await repo.list_paginated(account_id=acc.id, page=2, page_size=2)
        assert total == 5
        assert len(rows1) == 2
        assert len(rows2) == 2
        assert rows1[0].exec_id != rows2[0].exec_id


@pytest.mark.asyncio
async def test_trade_book_sync_non_overlapping():
    from app.db.models.account import AccountModel as AM
    from app.services.trade_book_sync_service import TradeBookSyncService

    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    svc = TradeBookSyncService(MagicMock(), mock_client, interval_sec=10.0)
    acc = MagicMock(spec=AM)
    acc.id = 1
    acc.ibkr_account = "DU123"
    acc.enabled = True
    lock = svc._lock_for(1)
    await lock.acquire()
    try:
        svc._current.fetch = AsyncMock()
        n, _ = await svc.sync_one_account(acc)
        assert n == 0
        svc._current.fetch.assert_not_called()
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_trade_book_no_flex_import():
    """Verify Flex removed from active path"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "app/broker/trade_book_sources.py").read_text()
    assert "flex_client" not in src.lower()
    assert "HistoricalExecutionSource" not in src
    src2 = (root / "app/services/trade_book_sync_service.py").read_text()
    assert "flex" not in src2.lower()
    assert "historical" not in src2.lower()
    src3 = (root / "app/core/config.py").read_text()
    assert "FLEX_WEB" not in src3


@pytest.mark.asyncio
async def test_order_book_status_filter(session_factory) -> None:
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"O{suffix}", ibkr_account=f"DUO{suffix.upper()}", total_margin=10000, enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        sig = SignalModel(strategy_id="model_blue", signal_id=f"SIG_{suffix}", trade_id=f"T_{suffix}", action="OPEN", pair="AAPL/MSFT", side="BUY", ref_price_a=Decimal(10), status="NEW", raw_payload={})
        session.add(sig)
        await session.commit()
        await session.refresh(sig)
        o1 = OrderModel(signal_id=sig.id, trade_id=sig.trade_id, internal_order_id=f"ORD-{suffix}-1", account_id=acc.id, strategy_id="model_blue", leg="L0", symbol="AAPL", ibkr_contract="AAPL-STK-SMART-USD", buy_sell="BUY", quantity=Decimal(10), limit_price=Decimal(100), status="FILLED")
        o2 = OrderModel(signal_id=sig.id, trade_id=sig.trade_id, internal_order_id=f"ORD-{suffix}-2", account_id=acc.id, strategy_id="model_blue", leg="L0", symbol="MSFT", ibkr_contract="MSFT-STK-SMART-USD", buy_sell="SELL", quantity=Decimal(5), limit_price=Decimal(200), status="REJECTED")
        session.add_all([o1, o2])
        await session.commit()
        from app.db.repositories.order_book_repository import OrderBookRepository
        repo = OrderBookRepository(session)
        rows, total = await repo.list_paginated(account_id=acc.id, page=1, page_size=10, status="FILLED")
        assert total == 1
        assert rows[0].status == "FILLED"


@pytest.mark.asyncio
async def test_trade_book_order_status_derived(session_factory) -> None:
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = AccountModel(name=f"OS{suffix}", ibkr_account=f"DUOS{suffix.upper()}", total_margin=10000, enabled=True)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        sig = SignalModel(strategy_id="model_blue", signal_id=f"SIGOS{suffix}", trade_id=f"TOS{suffix}", action="OPEN", pair="AAPL/MSFT", side="BUY", ref_price_a=Decimal(10), status="NEW", raw_payload={})
        session.add(sig)
        await session.commit()
        await session.refresh(sig)
        order = OrderModel(signal_id=sig.id, trade_id=sig.trade_id, internal_order_id=f"ORDOS{suffix}", account_id=acc.id, strategy_id="model_blue", leg="L0", symbol="AAPL", ibkr_contract="AAPL-STK-SMART-USD", buy_sell="BUY", quantity=Decimal(10), limit_price=Decimal(100), status="FILLED", broker_order_id="9001")
        session.add(order)
        await session.commit()
        await session.refresh(order)
        repo = TradeExecutionRepository(session)
        await repo.upsert_one(_line(f"exec.os.{suffix}", acct=acc.ibkr_account, broker_order_id=9001), account_id=acc.id, order_id=order.id)
        await session.commit()
        joined, total = await repo.list_paginated_with_order_status(account_id=acc.id, page=1, page_size=10)
        assert total == 1
        _, st = joined[0]
        assert st == "FILLED"
        # detached
        await repo.upsert_one(_line(f"exec.det.{suffix}", acct=acc.ibkr_account, broker_order_id=99999), account_id=acc.id, order_id=None)
        await session.commit()
        joined2, total2 = await repo.list_paginated_with_order_status(account_id=acc.id, page=1, page_size=10)
        assert total2 == 2
        _, detached_st = next(p for p in joined2 if p[0].exec_id == f"exec.det.{suffix}")
        assert detached_st is None


@pytest.mark.asyncio
async def test_trade_book_sync_current_only(session_factory):
    from unittest.mock import MagicMock as MM

    from app.db.models.account import AccountModel as AM
    from app.services.trade_book_sync_service import TradeBookSyncService
    client = MM()
    client.is_connected.return_value = True
    svc = TradeBookSyncService(MM(), client, interval_sec=10.0)
    acc = MM(spec=AM)
    acc.id = 999
    acc.ibkr_account = "DU999"
    svc._current.fetch = AsyncMock(return_value=type("R", (), {"lines": [_line("exec.cur.only", acct="DU999")], "timed_out": False})())
    svc._session_factory = session_factory
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as s:
        a = AccountModel(name=f"SC{suffix}", ibkr_account=f"DUSC{suffix.upper()}", total_margin=10000, enabled=True)
        s.add(a)
        await s.commit()
        await s.refresh(a)
        acc.id = a.id
        acc.ibkr_account = a.ibkr_account
        await svc.sync_one_account(acc)
        svc._current.fetch.assert_called_once()
