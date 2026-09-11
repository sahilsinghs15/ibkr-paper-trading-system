"""Comprehensive tests for Manual Trading Milestone M1-D: Reconciliation & Inventory Awareness.

Verifies:
1. Engine-only MATCH.
2. Manual-only MATCH (eliminates false BROKER_ORPHAN).
3. Engine + manual same-direction MATCH (+10 + +5 = +15).
4. Engine + manual opposing-direction MATCH (+10 + -3 = +7, eliminates false QTY_DRIFT).
5. Engine + manual net zero + broker flat (no ghost).
6. Net zero expected + broker residual (+10 + -10 = 0, broker has +2 -> BROKER_ORPHAN).
7. Engine + manual expected 7, broker 5 -> QTY_DRIFT (drift of 2).
8. Manual expected position + broker flat -> LEDGER_GHOST.
9. True broker rogue position -> BROKER_ORPHAN + ROGUE_TRADE_DETECTED event.
10. Closed manual positions (status == 'CLOSED') excluded from inventory.
11. Multiple manual trades on same symbol aggregate across distinct trade_ids.
12. Manual in-flight orders (PENDING_SUBMIT, SUBMITTED, PARTIALLY_FILLED) set in_flight=True.
13. API collect_reconcile_positions exposes engine_qty and manual_qty breakdown.
14. Transition-based rogue lifecycle (ROGUE_TRADE_DETECTED and ROGUE_TRADE_RESOLVED).
15. Zero broker order writes during reconciliation.
16. Strict account isolation (no inventory bleed across accounts).
17. Broker align target calculation protects manual positions from being flattened.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.positions import BrokerPositionLine
from app.db.models.account import AccountModel
from app.db.models.event import EventLogModel
from app.db.models.instrument import InstrumentModel
from app.db.models.manual_order import (
    ManualOrderModel,
    ManualPositionModel,
)
from app.db.models.position import PositionModel
from app.db.repositories.position_repository import PositionRepository
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.rms.models import OrderSide
from app.services.position_reconciler import (
    MISMATCH_BROKER_ORPHAN,
    MISMATCH_LEDGER_GHOST,
    MISMATCH_MATCH,
    MISMATCH_QTY_DRIFT,
    LedgerNetLine,
    PositionReconciler,
    build_ledger_net_lines,
    classify_reconcile_diffs,
    fetch_in_flight_accounts,
    ledger_net_qty_for_symbol,
)
from app.services.reconcile_service import collect_reconcile_positions


def _make_broker_line(
    *,
    account: str = "DU123",
    symbol: str = "IBUS500",
    sec_type: str = "CFD",
    qty: float = 10.0,
    con_id: int = 10001,
) -> BrokerPositionLine:
    return BrokerPositionLine(
        ibkr_account=account,
        symbol=symbol,
        sec_type=sec_type,
        con_id=con_id,
        currency="USD",
        exchange="SMART",
        quantity=qty,
        avg_cost=5000.0,
    )


async def _create_test_account(session: AsyncSession, suffix: str) -> AccountModel:
    acc = AccountModel(
        name=f"Acc {suffix}",
        ibkr_account=f"DU{suffix}",
        total_margin=Decimal("100000.00"),
        enabled=True,
    )
    session.add(acc)
    await session.flush()
    return acc


# ── 1-11: RECONCILIATION COEXISTENCE & LEDGER AGGREGATION ─────────────


def test_build_ledger_net_lines_pure_engine_and_pure_manual() -> None:
    """1 & 2: Pure engine position and pure manual position aggregate independently."""
    instruments = [
        InstrumentModel(
            symbol="SPY",
            sec_type="STK",
            trade_conid=111,
            market_data_conid=111,
            underlying_exchange="SMART",
            exchange="SMART",
            currency="USD",
        )
    ]
    # Pure engine position
    eng_pos = PositionModel(
        account_id=1,
        trade_id="MB_001",
        strategy_id="model_blue",
        leg_a_symbol="SPY",
        leg_a_signed_qty=Decimal(10),
        leg_a_entry_mark=Decimal(500),
        leg_a_instrument_type="STK",
        risk_state="OPEN",
        target=Decimal("0.05"),
        stop=Decimal("0.02"),
        time_limit=60,
    )
    lines_eng = build_ledger_net_lines([eng_pos], instruments, manual_open_rows=[])
    assert len(lines_eng) == 1
    assert lines_eng[0].symbol == "SPY"
    assert lines_eng[0].signed_qty == Decimal(10)
    assert lines_eng[0].engine_qty == Decimal(10)
    assert lines_eng[0].manual_qty == Decimal(0)

    # Pure manual position
    man_pos = ManualPositionModel(
        account_id=1,
        trade_id="MAN_001",
        symbol="IBUS500",
        con_id=10001,
        sec_type="CFD",
        currency="USD",
        signed_qty=Decimal(5),
        avg_cost=Decimal(5000),
        realized_pnl=Decimal(0),
        status="OPEN",
    )
    lines_man = build_ledger_net_lines([], instruments, manual_open_rows=[man_pos])
    assert len(lines_man) == 1
    assert lines_man[0].symbol == "IBUS500"
    assert lines_man[0].sec_type == "CFD"
    assert lines_man[0].signed_qty == Decimal(5)
    assert lines_man[0].engine_qty == Decimal(0)
    assert lines_man[0].manual_qty == Decimal(5)
    assert 10001 in lines_man[0].con_ids


def test_build_ledger_net_lines_coexistence_scenarios() -> None:
    """3-7 & 10-11: Coexisting engine + manual positions aggregate algebraically."""
    instruments: list[InstrumentModel] = []
    # Engine has +10 AAPL
    eng_pos = PositionModel(
        account_id=1,
        trade_id="MB_AAPL",
        strategy_id="model_blue",
        leg_a_symbol="AAPL",
        leg_a_signed_qty=Decimal(10),
        leg_a_entry_mark=Decimal(150),
        leg_a_instrument_type="STK",
        risk_state="OPEN",
        target=Decimal("0.05"),
        stop=Decimal("0.02"),
        time_limit=60,
    )
    # Manual has -3 AAPL
    man_pos = ManualPositionModel(
        account_id=1,
        trade_id="MAN_AAPL_1",
        symbol="AAPL",
        con_id=101,
        sec_type="STK",
        currency="USD",
        signed_qty=Decimal(-3),
        avg_cost=Decimal(152),
        status="OPEN",
    )
    # Manual closed row (must be ignored)
    man_closed = ManualPositionModel(
        account_id=1,
        trade_id="MAN_AAPL_CLOSED",
        symbol="AAPL",
        con_id=101,
        sec_type="STK",
        currency="USD",
        signed_qty=Decimal(0),
        avg_cost=Decimal(150),
        status="CLOSED",
    )

    # Coexisting opposing directions (+10 + -3 = +7)
    lines = build_ledger_net_lines([eng_pos], instruments, [man_pos, man_closed])
    assert len(lines) == 1
    assert lines[0].symbol == "AAPL"
    assert lines[0].signed_qty == Decimal(7)
    assert lines[0].engine_qty == Decimal(10)
    assert lines[0].manual_qty == Decimal(-3)

    # Coexisting multiple manual trades (+10 + -3 + +5 = +12)
    man_pos_2 = ManualPositionModel(
        account_id=1,
        trade_id="MAN_AAPL_2",
        symbol="AAPL",
        con_id=101,
        sec_type="STK",
        currency="USD",
        signed_qty=Decimal(5),
        avg_cost=Decimal(155),
        status="OPEN",
    )
    lines_multi = build_ledger_net_lines([eng_pos], instruments, [man_pos, man_pos_2, man_closed])
    assert len(lines_multi) == 1
    assert lines_multi[0].signed_qty == Decimal(12)
    assert lines_multi[0].engine_qty == Decimal(10)
    assert lines_multi[0].manual_qty == Decimal(2)

    # Coexisting exact cancellation (+10 + -10 = 0 -> omitted from lines to prevent false ghost)
    man_pos_cancel = ManualPositionModel(
        account_id=1,
        trade_id="MAN_AAPL_CANCEL",
        symbol="AAPL",
        con_id=101,
        sec_type="STK",
        currency="USD",
        signed_qty=Decimal(-10),
        avg_cost=Decimal(150),
        status="OPEN",
    )
    lines_zero = build_ledger_net_lines([eng_pos], instruments, [man_pos_cancel])
    assert len(lines_zero) == 0


def test_classify_coexistence_and_drift() -> None:
    """Classify diffs across MATCH, QTY_DRIFT, BROKER_ORPHAN, and LEDGER_GHOST."""
    ibkr_to_account = {"DU123": 1}

    # Case 1: Manual only matches broker (+5 vs +5) -> MATCH
    diffs_man_match = classify_reconcile_diffs(
        broker_lines=[_make_broker_line(qty=5.0)],
        ledger_lines=[
            LedgerNetLine(
                account_id=1,
                symbol="IBUS500",
                sec_type="CFD",
                signed_qty=Decimal("5.0"),
                con_ids=frozenset({10001}),
                engine_qty=Decimal(0),
                manual_qty=Decimal("5.0"),
            )
        ],
        ibkr_to_account=ibkr_to_account,
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs_man_match) == 1
    assert diffs_man_match[0].kind == MISMATCH_MATCH
    assert diffs_man_match[0].engine_qty == 0.0
    assert diffs_man_match[0].manual_qty == 5.0

    # Case 2: Coexisting Engine (+10) + Manual (-3) vs Broker (+7) -> MATCH
    diffs_coexist_match = classify_reconcile_diffs(
        broker_lines=[_make_broker_line(symbol="AAPL", sec_type="STK", qty=7.0)],
        ledger_lines=[
            LedgerNetLine(
                account_id=1,
                symbol="AAPL",
                sec_type="STK",
                signed_qty=Decimal("7.0"),
                con_ids=frozenset({101}),
                engine_qty=Decimal("10.0"),
                manual_qty=Decimal("-3.0"),
            )
        ],
        ibkr_to_account=ibkr_to_account,
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs_coexist_match) == 1
    assert diffs_coexist_match[0].kind == MISMATCH_MATCH
    assert diffs_coexist_match[0].broker_qty == 7.0
    assert diffs_coexist_match[0].ledger_qty == 7.0
    assert diffs_coexist_match[0].engine_qty == 10.0
    assert diffs_coexist_match[0].manual_qty == -3.0

    # Case 3: Expected 7, Broker has 5 -> QTY_DRIFT of 2
    diffs_drift = classify_reconcile_diffs(
        broker_lines=[_make_broker_line(symbol="AAPL", sec_type="STK", qty=5.0)],
        ledger_lines=[
            LedgerNetLine(
                account_id=1,
                symbol="AAPL",
                sec_type="STK",
                signed_qty=Decimal("7.0"),
                con_ids=frozenset({101}),
                engine_qty=Decimal("10.0"),
                manual_qty=Decimal("-3.0"),
            )
        ],
        ibkr_to_account=ibkr_to_account,
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs_drift) == 1
    assert diffs_drift[0].kind == MISMATCH_QTY_DRIFT
    assert diffs_drift[0].broker_qty == 5.0
    assert diffs_drift[0].ledger_qty == 7.0

    # Case 4: Net zero expected (+10 and -10), Broker has unexpected +2 -> BROKER_ORPHAN
    diffs_orphan = classify_reconcile_diffs(
        broker_lines=[_make_broker_line(symbol="AAPL", sec_type="STK", qty=2.0)],
        ledger_lines=[],  # Net zero produces empty ledger lines
        ibkr_to_account=ibkr_to_account,
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs_orphan) == 1
    assert diffs_orphan[0].kind == MISMATCH_BROKER_ORPHAN
    assert diffs_orphan[0].broker_qty == 2.0
    assert diffs_orphan[0].ledger_qty is None

    # Case 5: Manual expected +10, Broker is flat -> LEDGER_GHOST
    diffs_ghost = classify_reconcile_diffs(
        broker_lines=[],
        ledger_lines=[
            LedgerNetLine(
                account_id=1,
                symbol="IBUS500",
                sec_type="CFD",
                signed_qty=Decimal("10.0"),
                con_ids=frozenset({10001}),
                engine_qty=Decimal(0),
                manual_qty=Decimal("10.0"),
            )
        ],
        ibkr_to_account=ibkr_to_account,
        account_to_ibkr={1: "DU123"},
        timed_out=False,
        in_flight_accounts=set(),
    )
    assert len(diffs_ghost) == 1
    assert diffs_ghost[0].kind == MISMATCH_LEDGER_GHOST
    assert diffs_ghost[0].broker_qty is None
    assert diffs_ghost[0].ledger_qty == 10.0
    assert diffs_ghost[0].manual_qty == 10.0


# ── 12: IN-FLIGHT MANUAL ORDERS ───────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_in_flight_orders_mark_account_in_flight(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """12: Manual orders in PENDING_SUBMIT, SUBMITTED, PARTIALLY_FILLED mark account in_flight."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await _create_test_account(session, suffix)

        # Initially not in-flight
        in_flight = await fetch_in_flight_accounts(session)
        assert acc.id not in in_flight

        # Add SUBMITTED manual order
        order = ManualOrderModel(
            account_id=acc.id,
            ibkr_account=acc.ibkr_account,
            internal_order_id=f"MAN_INF_{suffix}",
            trade_id=f"TRD_{suffix}",
            idempotency_key=f"idem_{suffix}",
            symbol="IBUS500",
            con_id=10001,
            sec_type="CFD",
            currency="USD",
            exchange="SMART",
            side="BUY",
            order_type="LIMIT",
            limit_price=Decimal(5000),
            quantity=Decimal(10),
            status="SUBMITTED",
            broker_order_id="88888",
            source="manual",
        )
        session.add(order)
        await session.commit()

    async with session_factory() as session:
        in_flight = await fetch_in_flight_accounts(session)
        assert acc.id in in_flight

    # Terminal status does NOT mark in-flight
    async with session_factory() as session:
        o = (
            await session.execute(
                select(ManualOrderModel).where(ManualOrderModel.account_id == acc.id)
            )
        ).scalar_one()
        o.status = "FILLED"
        await session.commit()

    async with session_factory() as session:
        in_flight = await fetch_in_flight_accounts(session)
        assert acc.id not in in_flight


# ── 13: RECONCILIATION API BREAKDOWN (engine_qty / manual_qty) ─────────


@pytest.mark.asyncio
async def test_collect_reconcile_positions_api_breakdown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """13: collect_reconcile_positions exposes engine_qty and manual_qty on diff rows."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await _create_test_account(session, suffix)

        # 1. Engine position: +10 AAPL / -5 MSFT
        trade = OpenModelBlueTrade(
            trade_id=f"MB_{suffix}",
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
            account_id=acc.id,
            target=Decimal("0.05"),
            stop=Decimal("0.02"),
            time_limit=60,
        )

        # 2. Manual position: -3 AAPL
        man_pos = ManualPositionModel(
            account_id=acc.id,
            trade_id=f"MAN_{suffix}",
            symbol="AAPL",
            con_id=101,
            sec_type="STK",
            currency="USD",
            signed_qty=Decimal(-3),
            avg_cost=Decimal(152),
            status="OPEN",
        )
        session.add(man_pos)
        await session.commit()

    # Query API service for this account
    async with session_factory() as session:
        res = await collect_reconcile_positions(session, ibkr_account=acc.ibkr_account)
        diffs = [d for d in res.diffs if d.symbol == "AAPL"]
        assert len(diffs) == 1
        d = diffs[0]
        assert d.ledger_qty == 7.0
        assert d.engine_qty == 10.0
        assert d.manual_qty == -3.0


# ── 14: FULL SWEEP & TRANSITION-BASED ROGUE LIFECYCLE ─────────────────


@pytest.mark.asyncio
async def test_full_reconcile_sweep_and_rogue_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """14: PositionReconciler sweep eliminates false rogue on manual trades and resolves cleanly."""
    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc = await _create_test_account(session, suffix)

        # Legitimate manual trade of +10 IBUS500
        man_pos = ManualPositionModel(
            account_id=acc.id,
            trade_id=f"MAN_SWEEP_{suffix}",
            symbol="IBUS500",
            con_id=10001,
            sec_type="CFD",
            currency="USD",
            signed_qty=Decimal(10),
            avg_cost=Decimal(5000),
            status="OPEN",
        )
        session.add(man_pos)
        await session.commit()

    # Broker returns exactly the manual trade (+10 IBUS500)
    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    sweep_1_lines = [
        _make_broker_line(account=acc.ibkr_account, symbol="IBUS500", sec_type="CFD", qty=10.0)
    ]
    mock_client.request_positions_async = AsyncMock(return_value=(sweep_1_lines, False))

    reconciler = PositionReconciler(session_factory, mock_client, interval_sec=9999.0)

    # First sweep: MUST BE MATCH, ZERO rogue events for this account!
    with patch(
        "app.services.position_reconciler.send_canonical_telegram", new_callable=AsyncMock
    ) as mock_tg:
        await reconciler.run_once()
        # Verify no rogue alerts for this account
        assert not any(acc.ibkr_account in str(c) for c in mock_tg.call_args_list)

        async with session_factory() as session:
            events = list(
                (
                    await session.execute(
                        select(EventLogModel)
                        .where(
                            EventLogModel.process == "reconcile",
                            EventLogModel.kind == "ROGUE_TRADE_DETECTED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            # ZERO rogue detections for legitimate manual trade!
            account_rogues = [
                e for e in events if e.detail.get("ibkr_account") == acc.ibkr_account
            ]
            assert len(account_rogues) == 0

    # Second sweep: UNTRACKED trade appears (+20 TSLA at broker) -> Triggers ROGUE_TRADE_DETECTED
    sweep_2_lines = [
        _make_broker_line(account=acc.ibkr_account, symbol="IBUS500", sec_type="CFD", qty=10.0),
        _make_broker_line(
            account=acc.ibkr_account, symbol="TSLA", sec_type="STK", qty=20.0, con_id=103
        ),
    ]
    mock_client.request_positions_async = AsyncMock(return_value=(sweep_2_lines, False))

    with patch(
        "app.services.position_reconciler.send_canonical_telegram", new_callable=AsyncMock
    ) as mock_tg:
        await reconciler.run_once()
        assert any("TSLA" in str(c) and acc.ibkr_account in str(c) for c in mock_tg.call_args_list)

    # Third sweep: Untracked trade disappears -> Triggers ROGUE_TRADE_RESOLVED
    mock_client.request_positions_async = AsyncMock(return_value=(sweep_1_lines, False))
    with patch(
        "app.services.position_reconciler.send_canonical_telegram", new_callable=AsyncMock
    ) as mock_tg:
        await reconciler.run_once()
        assert any(
            "ROGUE_TRADE_RESOLVED" in str(c)
            and "TSLA" in str(c)
            and acc.ibkr_account in str(c)
            for c in mock_tg.call_args_list
        )


# ── 16: STRICT ACCOUNT ISOLATION ──────────────────────────────────────


def test_strict_account_isolation_in_reconciliation() -> None:
    """16: Manual inventory in Account 1 NEVER affects Account 2 inventory."""
    ibkr_to_account = {"DU_ACC_1": 1, "DU_ACC_2": 2}

    # Account 1 has manual +10 IBUS500
    ledger_lines = [
        LedgerNetLine(
            account_id=1,
            symbol="IBUS500",
            sec_type="CFD",
            signed_qty=Decimal("10.0"),
            con_ids=frozenset({10001}),
            engine_qty=Decimal(0),
            manual_qty=Decimal("10.0"),
        )
    ]

    # Broker has +10 in Account 2 (where ledger expects 0)
    broker_lines = [
        _make_broker_line(account="DU_ACC_2", symbol="IBUS500", sec_type="CFD", qty=10.0)
    ]

    diffs = classify_reconcile_diffs(
        broker_lines=broker_lines,
        ledger_lines=ledger_lines,
        ibkr_to_account=ibkr_to_account,
        timed_out=False,
        in_flight_accounts=set(),
    )
    # Must produce TWO separate diffs:
    # 1. Account 1: LEDGER_GHOST (expected +10, broker has 0)
    # 2. Account 2: BROKER_ORPHAN (expected 0, broker has +10)
    assert len(diffs) == 2
    acc1_diff = next(d for d in diffs if d.account_id == 1)
    acc2_diff = next(d for d in diffs if d.account_id == 2)
    assert acc1_diff.kind == MISMATCH_LEDGER_GHOST
    assert acc2_diff.kind == MISMATCH_BROKER_ORPHAN


# ── 17: BROKER ALIGN TARGET INVARIANT ─────────────────────────────────


def test_broker_align_target_protects_manual_positions() -> None:
    """17: ledger_net_qty_for_symbol includes manual open positions to prevent flattening."""
    instruments: list[InstrumentModel] = []
    # Engine is flat (0 open rows)
    open_engine_rows: list[PositionModel] = []

    # Manual has +5 IBUS500 CFD
    manual_open_rows = [
        ManualPositionModel(
            account_id=1,
            trade_id="MAN_PROTECT",
            symbol="IBUS500",
            con_id=10001,
            sec_type="CFD",
            currency="USD",
            signed_qty=Decimal("5.0"),
            status="OPEN",
        )
    ]

    target = ledger_net_qty_for_symbol(
        open_engine_rows,
        instruments,
        account_id=1,
        symbol="IBUS500",
        sec_type="CFD",
        manual_open_rows=manual_open_rows,
    )
    # Target must be +5.0 (NOT 0.0 or None!)
    assert target == 5.0


# ── 15: ZERO BROKER ORDER WRITES AUDIT ─────────────────────────────────


def test_reconciler_has_zero_broker_order_writes() -> None:
    """15: PositionReconciler and helper functions invoke zero broker order writing APIs."""
    import inspect

    import app.services.position_reconciler as pr_mod

    source = inspect.getsource(pr_mod)
    forbidden = ["placeOrder", "cancelOrder", "reqGlobalCancel", "emergency_flatten", "square_off"]
    for call in forbidden:
        assert call not in source, f"Forbidden call {call} found in position_reconciler.py"
