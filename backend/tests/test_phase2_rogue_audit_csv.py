"""Tests for Phase 2: Active Rogue Trade lifecycle, Audit Logs, and Closed Trades CSV export."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.positions import BrokerPositionLine
from app.core.security import create_access_token
from app.db.models.account import AccountModel
from app.db.models.event import EventLogModel
from app.db.models.instrument import InstrumentModel
from app.db.models.position import PositionModel
from app.db.models.user import UserModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.position_repository import PositionRepository
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.rms.models import OrderSide
from app.services.position_reconciler import (
    MISMATCH_BROKER_ORPHAN,
    MISMATCH_LEDGER_GHOST,
    MISMATCH_QTY_DRIFT,
    PositionReconciler,
)
from demo_streaming.api import create_demo_app


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


@pytest.mark.asyncio
async def test_rogue_trade_detection_deduplication_and_resolution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Test all three rogue types (QTY_DRIFT, BROKER_ORPHAN, LEDGER_GHOST), deduplication across sweeps, and resolution."""
    uid = uuid4().hex[:6]
    ibkr_account = f"DU{uid}"

    async with session_factory() as session:
        acc = AccountModel(
            name=f"Acc {uid}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add(acc)
        for sym, cid in [("AAPL", 101), ("MSFT", 102)]:
            existing = (
                await session.execute(
                    select(InstrumentModel).where(InstrumentModel.symbol == sym)
                )
            ).scalar_one_or_none()
            if not existing:
                session.add(
                    InstrumentModel(
                        symbol=sym,
                        sec_type="STK",
                        trade_conid=cid,
                        market_data_conid=cid,
                        underlying_exchange="SMART",
                        exchange="SMART",
                        currency="USD",
                    )
                )
        await session.flush()
        account_id = acc.id

        # Open trade in ledger: AAPL +10, MSFT -5
        trade = OpenModelBlueTrade(
            trade_id=f"MB_{uid}",
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
        await session.commit()

    # Client returns:
    # 1. AAPL quantity 15 (Ledger is 10) -> QTY_DRIFT
    # 2. TSLA quantity 20 (Not in ledger) -> BROKER_ORPHAN
    # (MSFT is in ledger -5, but broker returns nothing -> LEDGER_GHOST)
    client = MagicMock()
    client.is_connected.return_value = True
    sweep_1_broker_lines = [
        _broker_line(
            account=ibkr_account, symbol="AAPL", sec_type="STK", qty=15.0, con_id=101
        ),
        _broker_line(
            account=ibkr_account, symbol="TSLA", sec_type="STK", qty=20.0, con_id=103
        ),
    ]
    client.request_positions_async = AsyncMock(
        return_value=(sweep_1_broker_lines, False)
    )

    reconciler = PositionReconciler(session_factory, client, interval_sec=9999.0)

    # First sweep -> Detect all three rogue conditions
    with patch(
        "app.services.position_reconciler.send_canonical_telegram",
        new_callable=AsyncMock,
    ) as mock_tg:
        await reconciler.run_once()

        async with session_factory() as session:
            events = list(
                (
                    await session.execute(
                        select(EventLogModel)
                        .where(EventLogModel.process == "reconcile")
                        .order_by(EventLogModel.id)
                    )
                )
                .scalars()
                .all()
            )

        detected_events = [
            e
            for e in events
            if e.kind == "ROGUE_TRADE_DETECTED"
            and e.detail.get("ibkr_account") == ibkr_account
        ]
        assert len(detected_events) == 3

        types_detected = {
            e.detail.get("rogue_type"): e.detail.get("symbol") for e in detected_events
        }
        assert types_detected.get(MISMATCH_QTY_DRIFT) == "AAPL"
        assert types_detected.get(MISMATCH_BROKER_ORPHAN) == "TSLA"
        assert types_detected.get(MISMATCH_LEDGER_GHOST) == "MSFT"

        acc_calls = [
            c
            for c in mock_tg.call_args_list
            if c[0][1].get("ibkr_account") == ibkr_account
        ]
        assert len(acc_calls) == 3

    # Second sweep -> Unchanged conditions. Must NOT produce duplicate detection events or telegram alerts!
    with patch(
        "app.services.position_reconciler.send_canonical_telegram",
        new_callable=AsyncMock,
    ) as mock_tg:
        await reconciler.run_once()

        async with session_factory() as session:
            events = list(
                (
                    await session.execute(
                        select(EventLogModel)
                        .where(EventLogModel.process == "reconcile")
                        .order_by(EventLogModel.id)
                    )
                )
                .scalars()
                .all()
            )

        detected_events = [
            e
            for e in events
            if e.kind == "ROGUE_TRADE_DETECTED"
            and e.detail.get("ibkr_account") == ibkr_account
        ]
        # Still exactly 3!
        assert len(detected_events) == 3
        acc_calls = [
            c
            for c in mock_tg.call_args_list
            if c[0][1].get("ibkr_account") == ibkr_account
        ]
        assert len(acc_calls) == 0

    # Third sweep -> Broker aligns perfectly: AAPL 10, MSFT -5, no TSLA
    sweep_3_broker_lines = [
        _broker_line(
            account=ibkr_account, symbol="AAPL", sec_type="STK", qty=10.0, con_id=101
        ),
        _broker_line(
            account=ibkr_account, symbol="MSFT", sec_type="STK", qty=-5.0, con_id=102
        ),
    ]
    client.request_positions_async = AsyncMock(
        return_value=(sweep_3_broker_lines, False)
    )

    with patch(
        "app.services.position_reconciler.send_canonical_telegram",
        new_callable=AsyncMock,
    ) as mock_tg:
        await reconciler.run_once()

        async with session_factory() as session:
            events = list(
                (
                    await session.execute(
                        select(EventLogModel)
                        .where(EventLogModel.process == "reconcile")
                        .order_by(EventLogModel.id)
                    )
                )
                .scalars()
                .all()
            )

        resolved_events = [
            e
            for e in events
            if e.kind == "ROGUE_TRADE_RESOLVED"
            and e.detail.get("ibkr_account") == ibkr_account
        ]
        # All 3 resolved
        assert len(resolved_events) == 3
        resolved_symbols = {e.detail.get("symbol") for e in resolved_events}
        assert resolved_symbols == {"AAPL", "MSFT", "TSLA"}
        acc_calls = [
            c
            for c in mock_tg.call_args_list
            if c[0][1].get("ibkr_account") == ibkr_account
        ]
        assert len(acc_calls) == 3


@pytest.mark.asyncio
async def test_rogue_restart_behavior(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Test that restarting PositionReconciler recovers active rogue state from previous runs without duplicate alerts."""
    uid = uuid4().hex[:6]
    ibkr_account = f"DU{uid}"

    async with session_factory() as session:
        acc = AccountModel(
            name=f"Acc {uid}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        repo = BrokerPositionRepository(session)
        # Pre-populate previous run with an active QTY_DRIFT
        await repo.insert_run(
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            timed_out=False,
            error=None,
            broker_line_count=1,
            match_count=0,
            ghost_count=0,
            orphan_count=1,
            drift_count=0,
            unmapped_account_count=0,
            mismatches=[
                {
                    "kind": MISMATCH_BROKER_ORPHAN,
                    "ibkr_account": ibkr_account,
                    "account_id": account_id,
                    "symbol": "NVDA",
                    "sec_type": "STK",
                    "con_id": 999,
                    "broker_qty": 50.0,
                    "ledger_qty": None,
                    "in_flight": False,
                }
            ],
        )
        await session.commit()

    client = MagicMock()
    client.is_connected.return_value = True
    # Still returning NVDA drift
    client.request_positions_async = AsyncMock(
        return_value=(
            [
                _broker_line(
                    account=ibkr_account,
                    symbol="NVDA",
                    sec_type="STK",
                    qty=50.0,
                    con_id=999,
                )
            ],
            False,
        )
    )

    # Fresh reconciler instance representing process restart
    reconciler = PositionReconciler(session_factory, client, interval_sec=9999.0)

    with patch(
        "app.services.position_reconciler.send_canonical_telegram",
        new_callable=AsyncMock,
    ) as mock_tg:
        await reconciler.run_once()

        async with session_factory() as session:
            events = list(
                (
                    await session.execute(
                        select(EventLogModel)
                        .where(EventLogModel.process == "reconcile")
                        .order_by(EventLogModel.id)
                    )
                )
                .scalars()
                .all()
            )

        # Because NVDA drift was already active in previous run before restart, NO duplicate detected event!
        detected = [
            e
            for e in events
            if e.kind == "ROGUE_TRADE_DETECTED"
            and e.detail.get("ibkr_account") == ibkr_account
            and e.detail.get("symbol") == "NVDA"
        ]
        assert len(detected) == 0
        acc_calls = [
            c
            for c in mock_tg.call_args_list
            if c[0][1].get("ibkr_account") == ibkr_account
        ]
        assert len(acc_calls) == 0


@pytest.mark.asyncio
async def test_multi_account_isolation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Test that rogue incidents for different accounts with the same symbol are isolated."""
    acc1_name = f"DUA_{uuid4().hex[:4]}"
    acc2_name = f"DUB_{uuid4().hex[:4]}"

    async with session_factory() as session:
        a1 = AccountModel(
            name="A1",
            ibkr_account=acc1_name,
            total_margin=Decimal(100000),
            enabled=True,
        )
        a2 = AccountModel(
            name="A2",
            ibkr_account=acc2_name,
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add_all([a1, a2])
        await session.commit()

    client = MagicMock()
    client.is_connected.return_value = True
    # Both accounts have orphan TSLA
    lines = [
        _broker_line(
            account=acc1_name, symbol="TSLA", sec_type="STK", qty=10.0, con_id=777
        ),
        _broker_line(
            account=acc2_name, symbol="TSLA", sec_type="STK", qty=20.0, con_id=777
        ),
    ]
    client.request_positions_async = AsyncMock(return_value=(lines, False))

    reconciler = PositionReconciler(session_factory, client, interval_sec=9999.0)
    with patch(
        "app.services.position_reconciler.send_canonical_telegram",
        new_callable=AsyncMock,
    ):
        await reconciler.run_once()

    assert reconciler._active_rogue_keys is not None
    keys = list(reconciler._active_rogue_keys.keys())
    assert (acc1_name, "TSLA", "STK", MISMATCH_BROKER_ORPHAN) in keys
    assert (acc2_name, "TSLA", "STK", MISMATCH_BROKER_ORPHAN) in keys


@pytest.mark.asyncio
async def test_audit_logs_api_authorization_and_queries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Test /demo/audit-logs authentication, admin authorization, pagination, category filtering, and search."""
    redis_mock = MagicMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.xread = AsyncMock(return_value=[])

    app = create_demo_app(
        session_factory=session_factory,
        redis=redis_mock,
        stream_name="positions:stream",
    )

    prefix = f"audit_test_{uuid4().hex[:6]}"
    async with session_factory() as session:
        admin_user = UserModel(
            email=f"admin_{prefix}@test.com",
            password_hash="mock",
            role="admin",
            is_active=True,
        )
        norm_user = UserModel(
            email=f"user_{prefix}@test.com",
            password_hash="mock",
            role="user",
            is_active=True,
        )
        session.add_all([admin_user, norm_user])
        await session.commit()
        await session.refresh(admin_user)
        await session.refresh(norm_user)

        repo = EventRepository(session)
        # Create events across processes and kinds
        await repo.append(
            process="webhook",
            kind="SIGNAL_RECEIVED",
            detail={"symbol": "AAPL", "message": f"{prefix} webhook signal"},
        )
        await repo.append(
            process="basket",
            kind="BASKET_SUBMITTED",
            detail={"symbol": "MSFT", "message": f"{prefix} basket submitted"},
        )
        await repo.append(
            process="rms",
            kind="RMS_PASSED",
            detail={"symbol": "GOOGL", "message": f"{prefix} rms passed"},
        )
        await repo.append(
            process="reconcile",
            kind="ROGUE_TRADE_DETECTED",
            detail={
                "symbol": "NVDA",
                "rogue_type": "QTY_DRIFT",
                "message": f"{prefix} rogue detected",
            },
        )
        await session.commit()

    admin_token = create_access_token({"sub": str(admin_user.id), "role": "admin"})
    user_token = create_access_token({"sub": str(norm_user.id), "role": "user"})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated -> 401
        res_unauth = await client.get(
            "/demo/audit-logs",
            headers={"Authorization": "Bearer invalid_token"},
        )
        assert res_unauth.status_code == 401

        # 2. Non-admin -> 403
        res_forbidden = await client.get(
            "/demo/audit-logs",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert res_forbidden.status_code == 403

        # 3. Admin -> 200 with server-side pagination
        res_admin = await client.get(
            "/demo/audit-logs",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"limit": 2, "offset": 0},
        )
        assert res_admin.status_code == 200
        data = res_admin.json()
        assert "total" in data
        assert len(data["events"]) <= 2
        assert data["limit"] == 2
        assert data["offset"] == 0

        # 4. Search by unique prefix
        res_search = await client.get(
            "/demo/audit-logs",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"search": prefix},
        )
        assert res_search.status_code == 200
        search_data = res_search.json()
        assert search_data["total"] == 4

        # 5. Category filter: signals -> webhook only
        res_signals = await client.get(
            "/demo/audit-logs",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"category": "signals", "search": prefix},
        )
        assert res_signals.status_code == 200
        signals_data = res_signals.json()
        assert signals_data["total"] == 1
        assert signals_data["events"][0]["kind"] == "SIGNAL_RECEIVED"

        # 6. Category filter: reconcile -> ROGUE_TRADE_DETECTED
        res_rec = await client.get(
            "/demo/audit-logs",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"category": "reconcile", "search": prefix},
        )
        assert res_rec.status_code == 200
        rec_data = res_rec.json()
        assert rec_data["total"] == 1
        assert rec_data["events"][0]["kind"] == "ROGUE_TRADE_DETECTED"


@pytest.mark.asyncio
async def test_closed_trades_csv_export(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Test /demo/closed-positions/csv export with streaming, authorization, headers, empty, and non-empty results."""
    redis_mock = MagicMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.xread = AsyncMock(return_value=[])

    app = create_demo_app(
        session_factory=session_factory,
        redis=redis_mock,
        stream_name="positions:stream",
    )

    prefix = f"csv_{uuid4().hex[:6]}"
    acc_name = f"DU{prefix}"

    async with session_factory() as session:
        acc = AccountModel(
            name=f"Acc {prefix}",
            ibkr_account=acc_name,
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        admin_user = UserModel(
            email=f"admin_{prefix}@test.com",
            password_hash="mock",
            role="admin",
            is_active=True,
        )
        norm_user = UserModel(
            email=f"user_{prefix}@test.com",
            password_hash="mock",
            role="user",
            ibkr_account_id=account_id,
            is_active=True,
        )
        session.add_all([admin_user, norm_user])

        # Insert 3 CLOSED positions
        now = datetime.now(UTC)
        for i in range(3):
            pos = PositionModel(
                account_id=account_id,
                trade_id=f"T_{prefix}_{i}",
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal(10),
                leg_a_entry_mark=Decimal("150.0"),
                leg_a_instrument_type="STK",
                leg_b_symbol="MSFT",
                leg_b_signed_qty=Decimal(-5),
                leg_b_entry_mark=Decimal("300.0"),
                leg_b_instrument_type="STK",
                realised_pnl=Decimal("125.50"),
                commission=Decimal("2.00"),
                live_pnl=Decimal(0),
                target=Decimal("50.0"),
                stop=Decimal("-25.0"),
                time_limit=60,
                target_unit="ABSOLUTE",
                stop_unit="ABSOLUTE",
                exit_reason="PROFIT_TARGET",
                exit_automation_enabled=True,
                risk_state="CLOSED",
                opened_at=now - timedelta(days=2),
                closed_at=now - timedelta(days=1),
            )
            session.add(pos)
        await session.commit()

    admin_token = create_access_token({"sub": str(admin_user.id), "role": "admin"})
    user_token = create_access_token({"sub": str(norm_user.id), "role": "user"})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated -> 401
        res_unauth = await client.get(
            "/demo/closed-positions/csv",
            headers={"Authorization": "Bearer invalid_token"},
        )
        assert res_unauth.status_code == 401

        # 2. Empty result check (filter by nonexistent account)
        res_empty = await client.get(
            "/demo/closed-positions/csv",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"ibkr_account": "NONEXISTENT_ACC"},
        )
        assert res_empty.status_code == 200
        assert "text/csv" in res_empty.headers.get("content-type", "")
        lines = [line for line in res_empty.text.strip().split("\n") if line.strip()]
        # Header only!
        assert len(lines) == 1
        assert "Trade ID" in lines[0]

        # 3. Export matching records for user
        res_user = await client.get(
            "/demo/closed-positions/csv",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert res_user.status_code == 200
        assert "text/csv" in res_user.headers.get("content-type", "")
        disposition = res_user.headers.get("content-disposition", "")
        assert "attachment" in disposition
        assert ".csv" in disposition

        reader = csv.DictReader(io.StringIO(res_user.text))
        rows = list(reader)
        assert len(rows) == 3
        assert rows[0]["Account"] == acc_name
        assert rows[0]["Leg A Symbol"] == "AAPL"
        assert rows[0]["Leg B Symbol"] == "MSFT"
        assert rows[0]["Realized PnL"] == "125.50000000"
        assert rows[0]["Commission"] == "2.00000000"
        assert rows[0]["Net PnL"] == "123.50000000"
        assert rows[0]["Exit Reason"] == "PROFIT_TARGET"


@pytest.mark.asyncio
async def test_closed_trades_csv_date_filters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify CSV export respects date_from/date_to filters passed from frontend."""
    redis_mock = MagicMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.xread = AsyncMock(return_value=[])

    app = create_demo_app(
        session_factory=session_factory,
        redis=redis_mock,
        stream_name="positions:stream",
    )

    prefix = f"csvdate_{uuid4().hex[:6]}"
    acc_name = f"DU{prefix}".upper()

    async with session_factory() as session:
        acc = AccountModel(
            name=f"Acc {prefix}",
            ibkr_account=acc_name,
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        admin_user = UserModel(
            email=f"admin_{prefix}@test.com",
            password_hash="mock",
            role="admin",
            is_active=True,
        )
        session.add(admin_user)

        now = datetime.now(UTC)
        # Old trade: closed 10 days ago
        for i in range(2):
            pos = PositionModel(
                account_id=account_id,
                trade_id=f"T_{prefix}_old_{i}",
                strategy_id="model_blue",
                leg_a_symbol="AAPL",
                leg_a_signed_qty=Decimal(10),
                leg_a_entry_mark=Decimal("150.0"),
                leg_a_instrument_type="STK",
                leg_b_symbol="MSFT",
                leg_b_signed_qty=Decimal(-5),
                leg_b_entry_mark=Decimal("300.0"),
                leg_b_instrument_type="STK",
                realised_pnl=Decimal("10.00"),
                commission=Decimal("1.00"),
                live_pnl=Decimal(0),
                target=Decimal("50.0"),
                stop=Decimal("-25.0"),
                time_limit=60,
                target_unit="ABSOLUTE",
                stop_unit="ABSOLUTE",
                exit_reason="PROFIT_TARGET",
                exit_automation_enabled=True,
                risk_state="CLOSED",
                opened_at=now - timedelta(days=12),
                closed_at=now - timedelta(days=10),
            )
            session.add(pos)
        # Recent trade: closed 1 day ago
        pos = PositionModel(
            account_id=account_id,
            trade_id=f"T_{prefix}_recent",
            strategy_id="model_blue",
            leg_a_symbol="AAPL",
            leg_a_signed_qty=Decimal(10),
            leg_a_entry_mark=Decimal("150.0"),
            leg_a_instrument_type="STK",
            leg_b_symbol="MSFT",
            leg_b_signed_qty=Decimal(-5),
            leg_b_entry_mark=Decimal("300.0"),
            leg_b_instrument_type="STK",
            realised_pnl=Decimal("20.00"),
            commission=Decimal("1.00"),
            live_pnl=Decimal(0),
            target=Decimal("50.0"),
            stop=Decimal("-25.0"),
            time_limit=60,
            target_unit="ABSOLUTE",
            stop_unit="ABSOLUTE",
            exit_reason="PROFIT_TARGET",
            exit_automation_enabled=True,
            risk_state="CLOSED",
            opened_at=now - timedelta(days=2),
            closed_at=now - timedelta(days=1),
        )
        session.add(pos)
        await session.commit()

    admin_token = create_access_token({"sub": str(admin_user.id), "role": "admin"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No date filter → all 3
        res_all = await client.get(
            "/demo/closed-positions/csv",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"ibkr_account": acc_name},
        )
        assert res_all.status_code == 200
        rows_all = list(csv.DictReader(io.StringIO(res_all.text)))
        assert len(rows_all) == 3

        # date_from 5 days ago → only recent (1)
        date_from = (now - timedelta(days=5)).isoformat()
        res_recent = await client.get(
            "/demo/closed-positions/csv",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"ibkr_account": acc_name, "date_from": date_from},
        )
        assert res_recent.status_code == 200
        rows_recent = list(csv.DictReader(io.StringIO(res_recent.text)))
        assert len(rows_recent) == 1
        assert rows_recent[0]["Trade ID"] == f"T_{prefix}_recent"

        # date_to 5 days ago → only old (2)
        date_to = (now - timedelta(days=5)).isoformat()
        res_old = await client.get(
            "/demo/closed-positions/csv",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"ibkr_account": acc_name, "date_to": date_to},
        )
        assert res_old.status_code == 200
        rows_old = list(csv.DictReader(io.StringIO(res_old.text)))
        assert len(rows_old) == 2
