"""Unit tests for IBKR position reconciler (snapshot + log, no ledger mutation)."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.positions import BrokerPositionLine, PositionSnapshotCollector
from app.broker.ibkr.tws_client import TWSClient
from app.db.models.account import AccountModel
from app.db.models.broker_position import BrokerPositionModel
from app.db.models.instrument import InstrumentModel
from app.db.models.position import PositionModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.position_repository import RISK_STATE_OPEN, PositionRepository
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.rms.models import OrderSide
from app.services.position_reconciler import (
    MISMATCH_BROKER_ORPHAN,
    MISMATCH_LEDGER_GHOST,
    MISMATCH_MATCH,
    MISMATCH_QTY_DRIFT,
    MISMATCH_UNMAPPED_ACCOUNT,
    LedgerNetLine,
    PositionReconciler,
    build_ledger_net_lines,
    classify_reconcile_diffs,
)
from app.services.reconcile_service import collect_reconcile_positions


def _broker_line(
    *,
    account: str = "DU123",
    symbol: str = "AAPL",
    sec_type: str = "CFD",
    qty: float = 100.0,
    con_id: int = 111,
) -> BrokerPositionLine:
    return BrokerPositionLine(
        ibkr_account=account,
        symbol=symbol,
        sec_type=sec_type,
        con_id=con_id,
        currency="USD",
        exchange="SMART",
        quantity=qty,
        avg_cost=150.0,
    )


def _ledger_line(
    *,
    account_id: int = 1,
    symbol: str = "AAPL",
    sec_type: str = "CFD",
    qty: float = 100.0,
) -> LedgerNetLine:
    return LedgerNetLine(
        account_id=account_id,
        symbol=symbol.upper(),
        sec_type=sec_type.upper(),
        signed_qty=Decimal(str(qty)),
        con_ids=frozenset({111}),
    )


def test_classify_match() -> None:
    diffs = classify_reconcile_diffs(
        broker_lines=[_broker_line(qty=100.0)],
        ledger_lines=[_ledger_line(qty=100.0)],
        ibkr_to_account={"DU123": 1},
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs) == 1
    assert diffs[0].kind == MISMATCH_MATCH


def test_classify_ghost_when_broker_flat() -> None:
    diffs = classify_reconcile_diffs(
        broker_lines=[],
        ledger_lines=[_ledger_line(qty=50.0)],
        ibkr_to_account={"DU123": 1},
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs) == 1
    assert diffs[0].kind == MISMATCH_LEDGER_GHOST


def test_classify_no_ghost_on_timeout() -> None:
    diffs = classify_reconcile_diffs(
        broker_lines=[],
        ledger_lines=[_ledger_line(qty=50.0)],
        ibkr_to_account={"DU123": 1},
        timed_out=True,
        in_flight_accounts=set(),
    )
    assert diffs == []


def test_classify_broker_orphan() -> None:
    diffs = classify_reconcile_diffs(
        broker_lines=[_broker_line(qty=25.0)],
        ledger_lines=[],
        ibkr_to_account={"DU123": 1},
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs) == 1
    assert diffs[0].kind == MISMATCH_BROKER_ORPHAN


def test_classify_qty_drift() -> None:
    diffs = classify_reconcile_diffs(
        broker_lines=[_broker_line(qty=100.0)],
        ledger_lines=[_ledger_line(qty=80.0)],
        ibkr_to_account={"DU123": 1},
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs) == 1
    assert diffs[0].kind == MISMATCH_QTY_DRIFT


def test_classify_unmapped_account() -> None:
    diffs = classify_reconcile_diffs(
        broker_lines=[_broker_line(account="UNKNOWN")],
        ledger_lines=[],
        ibkr_to_account={"DU123": 1},
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs) == 1
    assert diffs[0].kind == MISMATCH_UNMAPPED_ACCOUNT


def test_classify_in_flight_tag_still_logs() -> None:
    diffs = classify_reconcile_diffs(
        broker_lines=[_broker_line(qty=100.0)],
        ledger_lines=[_ledger_line(qty=80.0)],
        ibkr_to_account={"DU123": 1},
        timed_out=False,
        in_flight_accounts={1},
    )
    assert len(diffs) == 1
    assert diffs[0].kind == MISMATCH_QTY_DRIFT
    assert diffs[0].in_flight is True


def test_build_ledger_net_lines_from_pair_row() -> None:
    row = PositionModel(
        account_id=7,
        trade_id="T1",
        strategy_id="model_blue",
        leg_a_symbol="AAPL",
        leg_a_signed_qty=Decimal(100),
        leg_a_entry_mark=Decimal(150),
        leg_b_symbol="MSFT",
        leg_b_signed_qty=Decimal(-50),
        leg_b_entry_mark=Decimal(300),
        target=Decimal("0.05"),
        stop=Decimal("0.02"),
        time_limit=60,
        leg_a_instrument_type="STK",
        leg_b_instrument_type="STK",
        risk_state=RISK_STATE_OPEN,
    )
    instruments = [
        InstrumentModel(
            symbol="AAPL",
            sec_type="CFD",
            trade_conid=111,
            market_data_conid=111,
            underlying_exchange="NASDAQ",
            exchange="SMART",
            currency="USD",
            multiplier=Decimal(1),
        )
    ]
    nets = build_ledger_net_lines([row], instruments)
    by_symbol = {(n.account_id, n.symbol): n for n in nets}
    assert float(by_symbol[(7, "AAPL")].signed_qty) == 100.0
    assert float(by_symbol[(7, "MSFT")].signed_qty) == -50.0


def test_position_snapshot_collector_skips_zero_qty() -> None:
    collector = PositionSnapshotCollector()
    contract = MagicMock(symbol="AAPL", secType="CFD", conId=111, currency="USD", exchange="SMART")
    collector.on_position("DU1", contract, 0.0, 150.0)
    collector.on_position("DU1", contract, 100.0, 150.0)
    collector.on_position_end()
    lines = collector.snapshot()
    assert len(lines) == 1
    assert lines[0].quantity == 100.0


def test_tws_client_request_positions_returns_lines() -> None:
    client = TWSClient()
    collector = client._position_collector

    with (
        patch.object(client, "is_connected", return_value=True),
        patch.object(client, "reqPositions") as mock_req,
        patch.object(client, "cancelPositions"),
    ):
        def fire_positions() -> None:
            contract = MagicMock(symbol="AAPL", secType="CFD", conId=111, currency="USD", exchange="SMART")
            client.position("DU1", contract, 50.0, 150.0)
            client.positionEnd()

        mock_req.side_effect = fire_positions
        lines, timed_out = client.request_positions(timeout=1.0)

    assert timed_out is False
    assert len(lines) == 1
    assert lines[0].symbol == "AAPL"
    assert collector in client._listeners


@pytest.mark.asyncio
async def test_reconciler_skips_when_disconnected() -> None:
    client = MagicMock()
    client.is_connected.return_value = False
    session_factory = MagicMock()
    reconciler = PositionReconciler(session_factory, client)
    await reconciler.run_once()
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_reconciler_never_calls_position_repository_mutators(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reconciler run_once must not open or close Model Blue ledger rows."""
    client = MagicMock()
    client.is_connected.return_value = True
    client.request_positions_async = AsyncMock(return_value=([], False))

    reconciler = PositionReconciler(session_factory, client, interval_sec=9999.0)
    with (
        patch.object(PositionRepository, "open_trade", AsyncMock()) as mock_open,
        patch.object(PositionRepository, "close_trade", AsyncMock()) as mock_close,
    ):
        await reconciler.run_once()
        mock_open.assert_not_called()
        mock_close.assert_not_called()


@pytest.fixture
async def session_factory():
    from app.db.session import AsyncSessionLocal, engine

    yield AsyncSessionLocal
    await engine.dispose()


@pytest.mark.asyncio
async def test_broker_snapshot_replace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot(
            [
                {
                    "ibkr_account": "DU999",
                    "con_id": 1001,
                    "account_id": None,
                    "symbol": "AAPL",
                    "sec_type": "CFD",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal(10),
                    "avg_cost": Decimal(150),
                }
            ]
        )
        rows = list((await session.execute(select(BrokerPositionModel))).scalars().all())
        assert len(rows) == 1
        assert rows[0].con_id == 1001

        await repo.replace_snapshot(
            [
                {
                    "ibkr_account": "DU999",
                    "con_id": 2002,
                    "account_id": None,
                    "symbol": "MSFT",
                    "sec_type": "CFD",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal(5),
                    "avg_cost": Decimal(300),
                }
            ]
        )
        rows = list((await session.execute(select(BrokerPositionModel))).scalars().all())
        assert len(rows) == 1
        assert rows[0].con_id == 2002
        assert rows[0].symbol == "MSFT"


@pytest.mark.asyncio
async def test_reconcile_does_not_mutate_positions_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    trade_id = f"RECON-TEST-{test_id}"
    trade = OpenModelBlueTrade(
        trade_id=trade_id,
        strategy_id="model_blue",
        direction=1,
        legs=(
            OpenModelBlueTradeLeg(
                symbol="AAPL",
                instrument_type="STK",
                side=OrderSide.BUY,
                quantity=Decimal(10),
                price=Decimal(150),
            ),
            OpenModelBlueTradeLeg(
                symbol="MSFT",
                instrument_type="STK",
                side=OrderSide.SELL,
                quantity=Decimal(5),
                price=Decimal(300),
            ),
        ),
    )
    account_id: int
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"ReconAcc-{test_id}",
            ibkr_account=f"DU-RECON-{test_id}",
            total_margin=Decimal(100000),
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id
        await PositionRepository(session).open_trade(
            trade,
            account_id=account_id,
            target=Decimal("0.05"),
            stop=Decimal("0.02"),
            time_limit=60,
        )

    client = MagicMock()
    client.is_connected.return_value = True
    client.request_positions_async = AsyncMock(return_value=([], False))

    reconciler = PositionReconciler(session_factory, client, interval_sec=9999.0)
    await reconciler.run_once()

    async with session_factory() as session:
        row = await PositionRepository(session).get_open_by_trade_id(
            trade_id, account_id=account_id
        )
        assert row is not None
        assert row.risk_state == RISK_STATE_OPEN


@pytest.mark.asyncio
async def test_collect_reconcile_positions_ledger_ghost_when_broker_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-RECON-API-{test_id}"
    trade_id = f"RECON-API-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"ReconApiAcc-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        trade = OpenModelBlueTrade(
            trade_id=trade_id,
            strategy_id="model_blue",
            direction=1,
            legs=(
                OpenModelBlueTradeLeg(
                    symbol="AAPL",
                    instrument_type="STK",
                    side=OrderSide.BUY,
                    quantity=Decimal(10),
                    price=Decimal(150),
                ),
                OpenModelBlueTradeLeg(
                    symbol="MSFT",
                    instrument_type="STK",
                    side=OrderSide.SELL,
                    quantity=Decimal(5),
                    price=Decimal(300),
                ),
            ),
        )
        await PositionRepository(session).open_trade(
            trade,
            account_id=account_id,
            target=Decimal("0.05"),
            stop=Decimal("0.02"),
            time_limit=60,
        )

        repo = BrokerPositionRepository(session)
        await repo.insert_run(
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            timed_out=False,
            error=None,
            broker_line_count=0,
            match_count=0,
            ghost_count=1,
            orphan_count=0,
            drift_count=0,
            unmapped_account_count=0,
            mismatches=[],
        )

    async with session_factory() as session:
        payload = await collect_reconcile_positions(session, ibkr_account=ibkr_account)

    assert payload.run is not None
    assert payload.broker_positions == []
    assert len(payload.ledger_positions) == 1
    assert payload.ledger_positions[0].trade_id == trade_id
    assert payload.ledger_positions[0].risk_state == RISK_STATE_OPEN

    ghost_diffs = [d for d in payload.diffs if d.kind == MISMATCH_LEDGER_GHOST]
    assert len(ghost_diffs) >= 1
    assert any(d.symbol == "AAPL" for d in ghost_diffs)
    assert any(d.symbol == "MSFT" for d in ghost_diffs)
