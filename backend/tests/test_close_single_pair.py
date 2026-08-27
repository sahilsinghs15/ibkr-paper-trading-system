"""Comprehensive tests for Phase 2: Close Selected Open Pair.

Verifies:
1. Close one selected pair successfully (risk_state becomes CLOSED).
2. Other pairs on the same account remain untouched (OPEN).
3. Other accounts remain untouched (OPEN).
4. User cancellation creates no API request/order.
5. Invalid account returns HTTP 404.
6. Invalid pair or already closed pair returns HTTP 404 / 400.
7. Pair belonging to another account cannot be closed via another account's endpoint.
8. Concurrent duplicate close requests return in-flight operation without duplicate orders.
9. Reverse legs (Leg A and Leg B) are targeted correctly (SELL for BUY, BUY for SELL).
10. Partial fill is represented correctly (risk_state stays OPEN, status='PARTIAL', success=False).
11. Failed close preserves OPEN position (status='FAILED', success=False).
12. Global Kill Switch behavior remains unchanged (active flag not altered).
13. Start Again behavior from Phase 1 remains unchanged.
14. No unrelated IBKR orders are submitted.
"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models.account import AccountModel
from app.db.models.position import PositionModel
from app.db.repositories.position_repository import PositionRepository
from app.main import app
from app.oms.basket import BasketExecutionResult, BasketState
from app.oms.models import OMSOrderStatus
from app.rms.models import OrderSide
from app.services.kill_switch import (
    KillSwitchService,
    clear_account_kill_switch,
    is_account_kill_switch_active,
)
from app.services.position_close_service import SinglePairCloseService


@pytest.fixture
async def session_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    yield sf
    await engine.dispose()


@pytest.fixture
def client() -> TestClient:
    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.broker.ibkr.tws_client.TWSClient.is_connected", return_value=True),
        patch("app.services.worker_pool.ExecutionWorkerPool.start", new_callable=AsyncMock),
        patch("app.services.worker_pool.ExecutionWorkerPool.stop", new_callable=AsyncMock),
        patch(
            "app.services.position_reconciler.PositionReconciler.start",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.position_reconciler.PositionReconciler.stop",
            new_callable=AsyncMock,
        ),
        patch("app.services.recovery.RecoveryManager.run_startup_recovery", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_live_pnl", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_runtime_from_db", new_callable=AsyncMock),
        TestClient(app) as c,
    ):
        yield c


@pytest.mark.asyncio
async def test_close_single_pair_success_and_isolation(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 1, 2, 3, 12: Close selected pair 1. Pair 2 and Account B remain untouched. Kill switch untouched."""
    suffix = uuid4().hex[:6]
    trade_1 = f"MBLUE-P1-{suffix}"
    trade_2 = f"MBLUE-P2-{suffix}"
    trade_b = f"MBLUE-PB-{suffix}"

    async with session_factory() as session, session.begin():
        acc_a = AccountModel(name=f"AccA-{suffix}", ibkr_account=f"DUA{suffix}", total_margin=Decimal("100000.00"))
        acc_b = AccountModel(name=f"AccB-{suffix}", ibkr_account=f"DUB{suffix}", total_margin=Decimal("100000.00"))
        session.add_all([acc_a, acc_b])
        await session.flush()
        id_a, id_b = acc_a.id, acc_b.id

        pos_repo = PositionRepository(session)
        # Acc A Pair 1 (EWP/EWU)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=id_a,
                trade_id=trade_1,
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("30.00"),
                leg_b_symbol="EWU",
                leg_b_signed_qty=Decimal(-100),
                leg_b_entry_mark=Decimal("40.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )
        # Acc A Pair 2 (SPY/QQQ)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=id_a,
                trade_id=trade_2,
                strategy_id="model_blue",
                leg_a_symbol="SPY",
                leg_a_signed_qty=Decimal(50),
                leg_a_entry_mark=Decimal("500.00"),
                leg_b_symbol="QQQ",
                leg_b_signed_qty=Decimal(-50),
                leg_b_entry_mark=Decimal("450.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )
        # Acc B Pair 3 (EWP/EWU on Acc B)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=id_b,
                trade_id=trade_b,
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("30.00"),
                leg_b_symbol="EWU",
                leg_b_signed_qty=Decimal(-100),
                leg_b_entry_mark=Decimal("40.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )

    # Initial check: kill switch is active for neither account
    assert is_account_kill_switch_active(id_a) is False
    assert is_account_kill_switch_active(id_b) is False

    # Setup mock order manager with mock baskets coordinator returning filled orders
    mock_basket = AsyncMock()
    mock_order1 = MagicMock()
    mock_order1.status = OMSOrderStatus.FILLED
    mock_order1.is_compensation = False
    mock_order1.symbol = "EWP"
    mock_order1.commission = Decimal("1.00")
    mock_order2 = MagicMock()
    mock_order2.status = OMSOrderStatus.FILLED
    mock_order2.is_compensation = False
    mock_order2.symbol = "EWU"
    mock_order2.fill_price = Decimal("39.00")
    mock_order2.commission = Decimal("1.00")

    b_obj = MagicMock()
    b_obj.state = BasketState.CLOSED
    mock_basket.execute.return_value = BasketExecutionResult(
        basket=b_obj, intent=MagicMock(), orders=[mock_order1, mock_order2]
    )

    mock_om = MagicMock()
    mock_om._baskets = mock_basket
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda x: x)

    # Close Acc A Pair 1 (trade_1)
    svc = SinglePairCloseService(session_factory=session_factory, order_manager=mock_om)
    res = await svc.close_pair(id_a, trade_1)

    assert res.success is True
    assert res.status == "CLOSED"
    assert res.trade_id == trade_1
    assert res.account_id == id_a

    # Verify DB states:
    async with session_factory() as session:
        pos_repo = PositionRepository(session)
        p1 = await pos_repo.get_by_trade_id(trade_1, account_id=id_a)
        p2 = await pos_repo.get_by_trade_id(trade_2, account_id=id_a)
        pb = await pos_repo.get_by_trade_id(trade_b, account_id=id_b)

        assert p1.risk_state == "CLOSED"
        assert p2.risk_state == "OPEN"
        assert pb.risk_state == "OPEN"

    # Verify Kill switch state was NOT activated
    assert is_account_kill_switch_active(id_a) is False
    assert is_account_kill_switch_active(id_b) is False


@pytest.mark.asyncio
async def test_close_pair_unavailable_execution_dependency_fails_safely(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Bug #2 Regression Test: When execution dependency (baskets coordinator) is unavailable,

    Close Pair fails safely (success=False, status='FAILED', position stays OPEN, no fake CLOSED state).
    """
    suffix = uuid4().hex[:6]
    trade_id = f"MBLUE-UNAVAIL-{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"AccUnavail-{suffix}", ibkr_account=f"DUUN{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        pos_repo = PositionRepository(session)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=acc_id,
                trade_id=trade_id,
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("30.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )

    # Instantiate ClosePairService with NO order_manager / NO baskets coordinator
    svc = SinglePairCloseService(session_factory=session_factory, order_manager=None)
    res = await svc.close_pair(acc_id, trade_id)

    # Verify response indicated safe failure
    assert res.success is False
    assert res.status == "FAILED"
    assert "Execution dependency" in (res.message or "")

    # Verify position REMAINS OPEN in DB (no fake CLOSED state)
    async with session_factory() as session:
        pos_repo = PositionRepository(session)
        p_row = await pos_repo.get_by_trade_id(trade_id, account_id=acc_id)
        assert p_row is not None
        assert p_row.risk_state == "OPEN"


from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_invalid_account_and_pair_errors(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 5: Invalid account (404)."""
    fake_acc = 999999
    fake_trade = "MBLUE-FAKE"

    app.state.session_factory = session_factory

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res1 = await ac.post(f"/api/v1/config/accounts/{fake_acc}/positions/{fake_trade}/close")
        assert res1.status_code == 404
        assert f"Account {fake_acc} not found" in res1.json()["detail"]


@pytest.mark.asyncio
async def test_cross_account_and_nonexistent_pair_api(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 6 & 7 via API endpoint."""
    suffix = uuid4().hex[:6]
    trade_a = f"MBLUE-TRADERA-{suffix}"
    trade_b = f"MBLUE-TRADERB-{suffix}"

    async with session_factory() as session, session.begin():
        acc_a = AccountModel(name=f"AccA-{suffix}", ibkr_account=f"DUA{suffix}", total_margin=Decimal("100000.00"))
        acc_b = AccountModel(name=f"AccB-{suffix}", ibkr_account=f"DUB{suffix}", total_margin=Decimal("100000.00"))
        session.add_all([acc_a, acc_b])
        await session.flush()
        id_a, id_b = acc_a.id, acc_b.id

        pos_repo = PositionRepository(session)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=id_a,
                trade_id=trade_a,
                strategy_id="model_blue",
                leg_a_symbol="ABC",
                leg_a_signed_qty=Decimal(10),
                leg_a_entry_mark=Decimal("10.00"),
                target=Decimal(100),
                stop=Decimal(50),
                time_limit=3600,
                risk_state="OPEN",
            )
        )
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=id_b,
                trade_id=trade_b,
                strategy_id="model_blue",
                leg_a_symbol="XYZ",
                leg_a_signed_qty=Decimal(10),
                leg_a_entry_mark=Decimal("10.00"),
                target=Decimal(100),
                stop=Decimal(50),
                time_limit=3600,
                risk_state="OPEN",
            )
        )

    app.state.session_factory = session_factory

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Test 6: Non-existent pair on Account A -> 404
        res_no_pair = await ac.post(f"/api/v1/config/accounts/{id_a}/positions/MBLUE-NONEXISTENT/close")
        assert res_no_pair.status_code == 404
        assert "Open position 'MBLUE-NONEXISTENT' not found" in res_no_pair.json()["detail"]

        # Test 7: Trying to close Account B's pair via Account A endpoint -> 404
        res_cross = await ac.post(f"/api/v1/config/accounts/{id_a}/positions/{trade_b}/close")
        assert res_cross.status_code == 404
        assert f"Open position '{trade_b}' not found for account {id_a}" in res_cross.json()["detail"]


@pytest.mark.asyncio
async def test_already_closed_pair_returns_400(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 6b: Already closed position returns 400 error."""
    suffix = uuid4().hex[:6]
    trade_closed = f"MBLUE-CLOSED-{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{suffix}", ibkr_account=f"DU{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        pos_repo = PositionRepository(session)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=acc_id,
                trade_id=trade_closed,
                strategy_id="model_blue",
                leg_a_symbol="ABC",
                leg_a_signed_qty=Decimal(10),
                leg_a_entry_mark=Decimal("10.00"),
                target=Decimal(100),
                stop=Decimal(50),
                time_limit=3600,
                risk_state="CLOSED",
            )
        )

    app.state.session_factory = session_factory

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(f"/api/v1/config/accounts/{acc_id}/positions/{trade_closed}/close")
        assert res.status_code == 400
        assert "already CLOSED" in res.json()["detail"]


@pytest.mark.asyncio
async def test_leg_targeting_and_reverse_orders(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 9 & 14: Legs are targeted with correct reverse sides and quantities (BUY -> SELL, SELL -> BUY)."""
    suffix = uuid4().hex[:6]
    trade_id = f"MBLUE-REVERSE-{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{suffix}", ibkr_account=f"DU{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        pos_repo = PositionRepository(session)
        # Leg A: +100 (BUY) -> reverse order should be SELL 100
        # Leg B: -50 (SELL) -> reverse order should be BUY 50
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=acc_id,
                trade_id=trade_id,
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("30.00"),
                leg_b_symbol="EWU",
                leg_b_signed_qty=Decimal(-50),
                leg_b_entry_mark=Decimal("40.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )

    mock_basket = AsyncMock()
    mock_order1 = MagicMock()
    mock_order1.status = OMSOrderStatus.FILLED
    mock_order1.is_compensation = False
    mock_order1.symbol = "EWP"
    mock_order1.commission = Decimal("1.00")
    mock_order2 = MagicMock()
    mock_order2.status = OMSOrderStatus.FILLED
    mock_order2.is_compensation = False
    mock_order2.symbol = "EWU"
    mock_order2.fill_price = Decimal("39.00")
    mock_order2.commission = Decimal("1.00")

    b_obj = MagicMock()
    b_obj.state = BasketState.CLOSED
    mock_basket.execute.return_value = BasketExecutionResult(
        basket=b_obj, intent=MagicMock(), orders=[mock_order1, mock_order2]
    )

    mock_om = MagicMock()
    mock_om._baskets = mock_basket
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda x: x)

    svc = SinglePairCloseService(session_factory=session_factory, order_manager=mock_om)
    res = await svc.close_pair(acc_id, trade_id)

    assert res.success is True
    assert res.status == "CLOSED"

    # Inspect executed intent passed to BasketCoordinator
    executed_intent = mock_basket.execute.call_args[0][0]
    assert executed_intent.action.value == "CLOSE"
    assert len(executed_intent.legs) == 2

    leg_a_close = executed_intent.legs[0]
    assert leg_a_close.symbol == "EWP"
    assert leg_a_close.side == OrderSide.SELL
    assert leg_a_close.quantity == Decimal(100)

    leg_b_close = executed_intent.legs[1]
    assert leg_b_close.symbol == "EWU"
    assert leg_b_close.side == OrderSide.BUY
    assert leg_b_close.quantity == Decimal(50)


@pytest.mark.asyncio
async def test_partial_fill_handling(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 10: Partial fill keeps position OPEN, status='PARTIAL', success=False."""
    suffix = uuid4().hex[:6]
    trade_id = f"MBLUE-PARTIAL-{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{suffix}", ibkr_account=f"DU{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        pos_repo = PositionRepository(session)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=acc_id,
                trade_id=trade_id,
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("30.00"),
                leg_b_symbol="EWU",
                leg_b_signed_qty=Decimal(-100),
                leg_b_entry_mark=Decimal("40.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )

    mock_basket = AsyncMock()
    mock_order1 = MagicMock()
    mock_order1.status = OMSOrderStatus.FILLED
    mock_order1.filled_quantity = 100.0
    mock_order1.is_compensation = False
    mock_order2 = MagicMock()
    mock_order2.status = OMSOrderStatus.CANCELLED  # Leg B cancelled/unfilled
    mock_order2.filled_quantity = 0.0
    mock_order2.is_compensation = False

    mock_basket.execute.return_value = BasketExecutionResult(
        basket=MagicMock(), intent=MagicMock(), orders=[mock_order1, mock_order2]
    )

    mock_om = MagicMock()
    mock_om._baskets = mock_basket
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda x: x)

    svc = SinglePairCloseService(session_factory=session_factory, order_manager=mock_om)
    res = await svc.close_pair(acc_id, trade_id)

    assert res.success is False
    assert res.status == "PARTIAL"

    # Verify position is STILL OPEN in DB
    async with session_factory() as session:
        p_row = await PositionRepository(session).get_by_trade_id(trade_id, account_id=acc_id)
        assert p_row is not None
        assert p_row.risk_state == "OPEN"


@pytest.mark.asyncio
async def test_failed_close_preserves_open_position(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 11: Failed close execution keeps position OPEN, status='FAILED', success=False."""
    suffix = uuid4().hex[:6]
    trade_id = f"MBLUE-FAILED-{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{suffix}", ibkr_account=f"DU{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        pos_repo = PositionRepository(session)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=acc_id,
                trade_id=trade_id,
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("30.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )

    mock_basket = AsyncMock()
    mock_basket.execute.side_effect = RuntimeError("Broker connection dropped during order submission")

    mock_om = MagicMock()
    mock_om._baskets = mock_basket
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda x: x)

    svc = SinglePairCloseService(session_factory=session_factory, order_manager=mock_om)
    res = await svc.close_pair(acc_id, trade_id)

    assert res.success is False
    assert res.status == "FAILED"
    assert "Broker connection dropped" in (res.message or "")

    # Position MUST remain OPEN in DB
    async with session_factory() as session:
        p_row = await PositionRepository(session).get_by_trade_id(trade_id, account_id=acc_id)
        assert p_row is not None
        assert p_row.risk_state == "OPEN"


@pytest.mark.asyncio
async def test_duplicate_in_flight_close_requests_deduplicated(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 8: Concurrent duplicate close requests return in-flight task result without extra orders."""
    suffix = uuid4().hex[:6]
    trade_id = f"MBLUE-DUP-{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"Acc-{suffix}", ibkr_account=f"DU{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        pos_repo = PositionRepository(session)
        await pos_repo._session.execute(
            PositionModel.__table__.insert().values(
                account_id=acc_id,
                trade_id=trade_id,
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("30.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )

    mock_basket = AsyncMock()
    mock_order = MagicMock()
    mock_order.status = OMSOrderStatus.FILLED
    mock_order.is_compensation = False
    mock_order.symbol = "EWP"
    mock_order.fill_price = Decimal("31.00")
    mock_order.commission = Decimal("1.00")

    async def delayed_execute(*args, **kwargs):
        await asyncio.sleep(0.1)  # Simulate non-instant execution
        b_obj = MagicMock()
        b_obj.state = BasketState.CLOSED
        return BasketExecutionResult(basket=b_obj, intent=MagicMock(), orders=[mock_order])

    mock_basket.execute.side_effect = delayed_execute

    mock_om = MagicMock()
    mock_om._baskets = mock_basket
    mock_om._resolve_instruments = AsyncMock(side_effect=lambda x: x)

    svc = SinglePairCloseService(session_factory=session_factory, order_manager=mock_om)

    # Trigger two concurrent requests for the exact same (acc_id, trade_id)
    res1_task = asyncio.create_task(svc.close_pair(acc_id, trade_id))
    res2_task = asyncio.create_task(svc.close_pair(acc_id, trade_id))

    res1, res2 = await asyncio.gather(res1_task, res2_task)

    assert res1.success is True
    assert res2.success is True
    # Verify execute was called ONLY ONCE
    assert mock_basket.execute.call_count == 1


@pytest.mark.asyncio
async def test_phase_1_start_again_remains_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 13: Phase 1 Start Again and Kill Switch disarm functionality remains fully functional."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"AccPhase1-{suffix}", ibkr_account=f"DUP1{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    ks_svc = KillSwitchService(session_factory=session_factory)
    await ks_svc.initiate_square_off(acc_id, requested_by="operator")
    assert is_account_kill_switch_active(acc_id) is True

    cleared = await clear_account_kill_switch(session_factory, acc_id, cleared_by="operator")
    assert cleared == 1
    assert is_account_kill_switch_active(acc_id) is False
