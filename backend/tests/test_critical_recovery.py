"""Tests for CRITICAL basket auto-recovery and clear_critical latch."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.order import OrderModel
from app.db.models.signal import SignalModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.session import create_engine_from_settings
from app.oms.basket import BasketState
from app.oms.coordinator import BasketCoordinator
from app.services.critical_recovery import CriticalRecoveryService, parse_ibkr_contract


@pytest.mark.asyncio
async def test_clear_critical_unblocks_only_when_no_other_critical() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"crit-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            account_id = account.id
            session.add_all(
                [
                    BasketModel(
                        account_id=account_id,
                        trade_id="T-A",
                        strategy_id="synthetic_n_leg",
                        action="OPEN",
                        state=BasketState.CRITICAL.value,
                        intended_leg_count=2,
                        recovery_status="RECOVERING",
                    ),
                    BasketModel(
                        account_id=account_id,
                        trade_id="T-B",
                        strategy_id="synthetic_n_leg",
                        action="OPEN",
                        state=BasketState.CRITICAL.value,
                        intended_leg_count=2,
                        recovery_status="FAILED",
                    ),
                ]
            )

        coord = BasketCoordinator(MagicMock(), session_factory=factory)
        coord.mark_critical(account_id, "synthetic_n_leg")
        assert coord.is_open_blocked(account_id, "synthetic_n_leg") is True

        cleared = await coord.clear_critical(
            account_id=account_id,
            strategy_id="synthetic_n_leg",
            trade_id="T-A",
            action="OPEN",
            recovery_detail="test clear one",
        )
        assert cleared is True
        assert coord.is_open_blocked(account_id, "synthetic_n_leg") is True

        cleared_b = await coord.clear_critical(
            account_id=account_id,
            strategy_id="synthetic_n_leg",
            trade_id="T-B",
            action="OPEN",
            recovery_detail="test clear two",
        )
        assert cleared_b is True
        assert coord.is_open_blocked(account_id, "synthetic_n_leg") is False

        async with factory() as session:
            rows = (
                await session.execute(
                    select(BasketModel).where(BasketModel.account_id == account_id)
                )
            ).scalars().all()
            assert all(r.state == BasketState.RECOVERED.value for r in rows)
            assert all(r.recovery_status == "CLEARED" for r in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fail_critical_schedules_recovery() -> None:
    from tests.test_basket_coordinator import PlaceScript, _coord, _intent, _pass, _wired

    oms, _, _ = _wired(PlaceScript(["fill", "reject", "error"]))
    coord = _coord(oms, session_factory=None)
    recovery = MagicMock()
    recovery.schedule_recovery = MagicMock()
    coord.set_recovery_service(recovery)

    intent = _intent(["XLE", "XOP"], trade_id="T-SCHED", account_id=42)
    result = await coord.execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.CRITICAL
    recovery.schedule_recovery.assert_called_once_with(
        account_id=42,
        trade_id="T-SCHED",
        action="OPEN",
        strategy_id=intent.strategy_id,
    )


def test_parse_ibkr_contract_with_con_id() -> None:
    sym, sec, ex, cur, con = parse_ibkr_contract("XLE-STK-SMART-USD:12345")
    assert sym == "XLE"
    assert sec == "STK"
    assert ex == "SMART"
    assert cur == "USD"
    assert con == 12345


@pytest.mark.asyncio
async def test_recovery_clears_when_broker_flat(session_factory: async_sessionmaker[AsyncSession]) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-REC-{test_id}"
    con_id = 800000 + int(test_id[:4], 16) % 10000

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Rec-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal("100000"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id
        basket = BasketModel(
            account_id=account_id,
            trade_id=f"T-REC-{test_id}",
            strategy_id="synthetic_n_leg",
            action="OPEN",
            state=BasketState.CRITICAL.value,
            intended_leg_count=2,
            recovery_status="RECOVERING",
        )
        session.add(basket)
        await session.flush()
        basket_id = basket.id
        sig = SignalModel(
            signal_id=f"T-REC-{test_id}",
            strategy_id="synthetic_n_leg",
            trade_id=f"T-REC-{test_id}",
            action="OPEN",
            pair="AAPL",
            side="BUY",
            ref_price_a=Decimal("100"),
            raw_payload={"test": True},
            status="FAILED",
        )
        session.add(sig)
        await session.flush()
        session.add(
            OrderModel(
                signal_id=sig.id,
                trade_id=f"T-REC-{test_id}",
                internal_order_id=f"int-{test_id}",
                basket_id=basket_id,
                is_compensation=False,
                account_id=account_id,
                strategy_id="synthetic_n_leg",
                leg="L0",
                symbol="AAPL",
                ibkr_contract=f"AAPL-STK-SMART-USD:{con_id}",
                buy_sell="BUY",
                quantity=Decimal("10"),
                limit_price=Decimal("0"),
                status="FILLED",
                fill_qty=Decimal("10"),
            )
        )
        await BrokerPositionRepository(session).replace_snapshot(
            [
                {
                    "ibkr_account": ibkr_account,
                    "con_id": con_id,
                    "account_id": account_id,
                    "symbol": "AAPL",
                    "sec_type": "STK",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal("0"),
                    "avg_cost": Decimal("0"),
                }
            ],
            as_of=datetime.now(UTC),
        )

    client = MagicMock()
    client.is_connected.return_value = True
    client.request_positions_async = AsyncMock(return_value=([], False))

    coord = BasketCoordinator(MagicMock(), session_factory=session_factory)
    coord.mark_critical(account_id, "synthetic_n_leg")

    om = MagicMock()
    svc = CriticalRecoveryService(
        session_factory=session_factory,
        client=client,
        order_manager=om,
        coordinator=coord,
    )

    with patch.object(svc, "_flatten_leftovers", new_callable=AsyncMock) as mock_flatten:
        mock_flatten.return_value = ["con_id already flat"]
        with patch.object(svc, "_fetch_and_persist_snapshot", new_callable=AsyncMock) as mock_snap:
            mock_snap.return_value = True
            await svc._recover_once(
            account_id=account_id,
            trade_id=f"T-REC-{test_id}",
            action="OPEN",
            strategy_id="synthetic_n_leg",
            attempt=1,
        )

    assert coord.is_open_blocked(account_id, "synthetic_n_leg") is False
    async with session_factory() as session:
        row = (
            await session.execute(
                select(BasketModel).where(BasketModel.id == basket_id)
            )
        ).scalar_one()
        assert row.state == BasketState.RECOVERED.value
        assert row.recovery_status == "CLEARED"


@pytest.mark.asyncio
async def test_recovery_failed_leaves_critical_latched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-FAIL-{test_id}"
    con_id = 810000 + int(test_id[:4], 16) % 10000

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Fail-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal("100000"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id
        basket = BasketModel(
            account_id=account_id,
            trade_id=f"T-FAIL-{test_id}",
            strategy_id="synthetic_n_leg",
            action="OPEN",
            state=BasketState.CRITICAL.value,
            intended_leg_count=2,
        )
        session.add(basket)
        await session.flush()
        basket_id = basket.id
        sig = SignalModel(
            signal_id=f"T-FAIL-{test_id}",
            strategy_id="synthetic_n_leg",
            trade_id=f"T-FAIL-{test_id}",
            action="OPEN",
            pair="AAPL",
            side="BUY",
            ref_price_a=Decimal("100"),
            raw_payload={"test": True},
            status="FAILED",
        )
        session.add(sig)
        await session.flush()
        session.add(
            OrderModel(
                signal_id=sig.id,
                trade_id=f"T-FAIL-{test_id}",
                internal_order_id=f"int-f-{test_id}",
                basket_id=basket_id,
                is_compensation=False,
                account_id=account_id,
                strategy_id="synthetic_n_leg",
                leg="L0",
                symbol="AAPL",
                ibkr_contract=f"AAPL-STK-SMART-USD:{con_id}",
                buy_sell="BUY",
                quantity=Decimal("10"),
                limit_price=Decimal("0"),
                status="FILLED",
                fill_qty=Decimal("10"),
            )
        )
        await BrokerPositionRepository(session).replace_snapshot(
            [
                {
                    "ibkr_account": ibkr_account,
                    "con_id": con_id,
                    "account_id": account_id,
                    "symbol": "AAPL",
                    "sec_type": "STK",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal("5"),
                    "avg_cost": Decimal("150"),
                }
            ],
            as_of=datetime.now(UTC),
        )

    client = MagicMock()
    client.is_connected.return_value = True
    client.request_positions_async = AsyncMock(return_value=([], False))

    coord = BasketCoordinator(MagicMock(), session_factory=session_factory)
    coord.mark_critical(account_id, "synthetic_n_leg")

    svc = CriticalRecoveryService(
        session_factory=session_factory,
        client=client,
        order_manager=MagicMock(),
        coordinator=coord,
    )

    with patch.object(svc, "_flatten_leftovers", new_callable=AsyncMock) as mock_flatten:
        mock_flatten.return_value = ["con_id=810: FAILED"]
        with patch.object(svc, "_fetch_and_persist_snapshot", new_callable=AsyncMock) as mock_snap:
            mock_snap.return_value = True
            await svc._recover_once(
            account_id=account_id,
            trade_id=f"T-FAIL-{test_id}",
            action="OPEN",
            strategy_id="synthetic_n_leg",
            attempt=2,
        )

    assert coord.is_open_blocked(account_id, "synthetic_n_leg") is True
    async with session_factory() as session:
        row = (
            await session.execute(
                select(BasketModel).where(BasketModel.id == basket_id)
            )
        ).scalar_one()
        assert row.state == BasketState.CRITICAL.value
        assert row.recovery_status == "FAILED"


@pytest.mark.asyncio
async def test_recovery_retries_inside_same_in_flight_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = CriticalRecoveryService(
        session_factory=MagicMock(),
        client=MagicMock(),
    )
    attempts: list[int] = []

    async def fake_recover_once(**kwargs: object) -> str:
        attempts.append(int(kwargs["attempt"]))
        return "retry" if len(attempts) == 1 else "done"

    monkeypatch.setattr(svc, "_recover_once", fake_recover_once)
    monkeypatch.setattr(
        "app.services.critical_recovery.RECOVERY_RETRY_DELAY_SEC",
        0.0,
    )
    monkeypatch.setattr(
        "app.services.critical_recovery.MAX_RECOVERY_ATTEMPTS",
        2,
    )

    svc.schedule_recovery(
        account_id=1,
        trade_id="T-RETRY",
        action="OPEN",
        strategy_id="synthetic_n_leg",
    )
    key = (1, "T-RETRY", "OPEN")
    await svc._in_flight[key]

    assert attempts == [1, 2]
    assert key not in svc._in_flight


@pytest.mark.asyncio
async def test_connected_partial_fill_recovery_marks_critical(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.broker.ibkr.tws_client import TWSClient
    from app.db.models.signal import SignalModel
    from app.oms.ibkr_adapter import IBKRExecutionAdapter
    from app.oms.oms_service import OMSService
    from unittest.mock import MagicMock

    test_id = uuid4().hex[:8]
    trade_id = f"T-PARTIAL-{test_id}"
    async with session_factory() as session, session.begin():
        account = AccountModel(
            name=f"partial-{test_id}",
            ibkr_account=f"DU-P-{test_id}",
            total_margin=Decimal("100000"),
            enabled=True,
        )
        session.add(account)
        await session.flush()
        account_id = account.id
        basket = BasketModel(
            account_id=account_id,
            trade_id=trade_id,
            strategy_id="synthetic_n_leg",
            action="OPEN",
            state=BasketState.EXECUTING.value,
            intended_leg_count=2,
        )
        session.add(basket)
        await session.flush()
        basket_id = basket.id
        sig = SignalModel(
            signal_id=trade_id,
            strategy_id="synthetic_n_leg",
            trade_id=trade_id,
            action="OPEN",
            pair="XLE/XOP",
            side="BUY",
            ref_price_a=Decimal("100"),
            raw_payload={"test": True},
            status="FAILED",
        )
        session.add(sig)
        await session.flush()
        session.add(
            OrderModel(
                signal_id=sig.id,
                trade_id=trade_id,
                internal_order_id=f"int-p-{test_id}",
                basket_id=basket_id,
                is_compensation=False,
                account_id=account_id,
                strategy_id="synthetic_n_leg",
                leg="L0",
                symbol="XLE",
                ibkr_contract="XLE-STK-SMART-USD:111",
                buy_sell="BUY",
                quantity=Decimal("100"),
                limit_price=Decimal("0"),
                status="PARTIALLY_FILLED",
                fill_qty=Decimal("40"),
            )
        )

    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    oms = OMSService(adapter=adapter)
    coord = BasketCoordinator(oms, session_factory=session_factory)
    recovery = MagicMock()
    recovery.schedule_recovery = MagicMock()
    coord.set_recovery_service(recovery)

    await coord.recover_incomplete_baskets()

    assert coord.is_open_blocked(account_id, "synthetic_n_leg") is True
    recovery.schedule_recovery.assert_called_once_with(
        account_id=account_id,
        trade_id=trade_id,
        action="OPEN",
        strategy_id="synthetic_n_leg",
    )
    async with session_factory() as session:
        row = (
            await session.execute(
                select(BasketModel).where(BasketModel.id == basket_id)
            )
        ).scalar_one()
        assert row.state == BasketState.CRITICAL.value

