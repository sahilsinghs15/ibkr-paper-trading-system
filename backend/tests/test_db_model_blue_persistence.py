"""DB-2 persistence: Model Blue trades survive new sessions/OrderManager instances."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.context import AccountExecutionContext
from app.accounts.router import StaticStrategyAccountRouter
from app.broker.ibkr.tws_client import TWSClient
from app.db.models import (
    AccountModel,
    AllocationModel,
    OrderModel,
    StrategyModel,
)
from app.db.repositories.allocation_repository import AllocationRepository
from app.db.repositories.position_repository import PositionRepository
from app.db.repositories.signal_repository import SignalRepository
from app.db.session import create_engine_from_settings
from app.models.signal import Signal, SignalLeg, SignalType
from app.oms import IBKRExecutionAdapter, OMSService
from app.rms import RMSContext, RMSEngine
from app.rms.models import OrderSide, StrategyConfig
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.model_blue.db_allocation import DatabaseCommittedCapitalProvider
from app.services.model_blue.db_trade_book import DatabaseModelBlueTradeBook
from app.services.model_blue.parser import (
    MODEL_BLUE_STRATEGY_ID,
    parse_model_blue_payload,
)
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.model_blue.sizer import ModelBlueSizer
from app.services.order_manager import OrderManager

_COMMITTED = Decimal(25000)
_TS = datetime(2026, 8, 17, 19, 55, tzinfo=UTC)

XLE_XOP_OPEN = {
    "market": "SMART",
    "strategy": "model_blue",
    "action": "OPEN",
    "trade_id": "",
    "direction": 1,
    "buckets": [
        {
            "underlying": "XLE",
            "legs": [
                {"instrument_type": "STK", "side": "BUY", "weight": 0.5943, "price": 62.59}
            ],
        },
        {
            "underlying": "XOP",
            "legs": [
                {"instrument_type": "STK", "side": "SELL", "weight": -0.4057, "price": 183.34}
            ],
        },
    ],
}


@pytest.fixture
async def db_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_allocation(
    session: AsyncSession, *, committed: Decimal = _COMMITTED
) -> AccountModel:
    result = await session.execute(
        select(StrategyModel).where(StrategyModel.strategy_id == MODEL_BLUE_STRATEGY_ID)
    )
    strategy = result.scalar_one_or_none()
    if strategy is None:
        strategy = StrategyModel(
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            legs=2,
            expression="STK",
            max_open_positions=10,
            weight_source="payload",
            enabled=True,
        )
        session.add(strategy)
        await session.flush()

    account = AccountModel(
        name=f"paper-{uuid4().hex[:8]}",
        ibkr_account=f"DU{uuid4().hex[:8]}",
        total_margin=committed,
        enabled=True,
    )
    session.add(account)
    await session.flush()

    session.add(
        AllocationModel(
            account_id=account.id,
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            alloc_pct=Decimal(1),
            target=Decimal("500.00"),
            stop=Decimal("250.00"),
            time_limit=3600,
            enabled=True,
        )
    )
    await session.flush()
    return account


def _oms() -> OMSService:
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 200
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    from tests.ibkr_test_utils import fill_on_place_order

    fill_on_place_order(adapter, tws)
    return OMSService(adapter=adapter)


def _static_router(account: AccountModel) -> StaticStrategyAccountRouter:
    return StaticStrategyAccountRouter(
        [
            AccountExecutionContext(
                account_id=account.id,
                ibkr_account=account.ibkr_account,
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                total_margin=account.total_margin,
                alloc_pct=Decimal(1),
                committed_notional=account.total_margin,
                target=Decimal("500.00"),
                stop=Decimal("250.00"),
                time_limit=3600,
                max_open_positions=10,
            )
        ]
    )


def _order_manager(
    factory: async_sessionmaker[AsyncSession],
    oms: OMSService,
    *,
    account_id: int,
    account: AccountModel | None = None,
) -> OrderManager:
    router = _static_router(account) if account is not None else None
    return OrderManager(
        oms=oms,
        order_type="MARKET",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        rms_engine=RMSEngine(),
        rms_context=RMSContext(
            strategy_configs={
                MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    max_open_positions=10,
                    money_limit_per_symbol=Decimal(1_000_000),
                )
            }
        ),
        committed_capital_provider=DatabaseCommittedCapitalProvider(
            factory, account_id=account_id
        ),
        model_blue_trade_book=DatabaseModelBlueTradeBook(factory, account_id=account_id),
        session_factory=factory,
        persistence=ModelBlueExecutionPersistence(factory, account_id=account_id),
        account_router=router,
    )


@pytest.mark.asyncio
async def test_a_open_persistence_survives_new_session(db_factory: async_sessionmaker[AsyncSession]) -> None:
    """TEST A: OPEN persists both legs; a new session still sees them."""
    trade_id = f"MBG-PERSIST-A-{uuid4().hex[:8]}"
    payload = {**XLE_XOP_OPEN, "trade_id": trade_id}

    async with db_factory() as session, session.begin():
        account = await _seed_allocation(session)

    oms = _oms()
    manager = _order_manager(db_factory, oms, account_id=account.id, account=account)
    signal = parse_model_blue_payload(payload, timestamp=_TS, reason="test-a")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    assert len(result.orders) == 2

    async with db_factory() as session:
        pos_repo = PositionRepository(session)
        trade = await pos_repo.get_open_trade(trade_id)
        assert trade is not None
        assert {leg.symbol for leg in trade.legs} == {"XLE", "XOP"}
        assert len(trade.legs) == 2
        row = await pos_repo.get_by_trade_id(trade_id)
        assert row is not None
        assert row.risk_state == "OPEN"
        assert row.closed_at is None
        orders = (await session.execute(select(OrderModel).where(OrderModel.trade_id == trade_id))).scalars().all()
        assert len(orders) == 2
        assert {o.symbol for o in orders} == {"XLE", "XOP"}


@pytest.mark.asyncio
async def test_b_close_recovery_after_new_order_manager(db_factory: async_sessionmaker[AsyncSession]) -> None:
    """TEST B: CLOSE after destroying the original OrderManager recovers both DB legs."""
    trade_id = f"MBG-PERSIST-B-{uuid4().hex[:8]}"
    payload = {**XLE_XOP_OPEN, "trade_id": trade_id}

    async with db_factory() as session, session.begin():
        account = await _seed_allocation(session)

    oms = _oms()
    opener = _order_manager(db_factory, oms, account_id=account.id, account=account)
    await opener.process_signal_execution(
        parse_model_blue_payload(payload, timestamp=_TS, reason="test-b-open")
    )
    del opener

    closer = _order_manager(db_factory, oms, account_id=account.id, account=account)
    await closer.hydrate_runtime_from_db()
    close_signal = parse_model_blue_payload(
        {
            "market": "SMART",
            "strategy": "model_blue",
            "action": "CLOSE",
            "trade_id": trade_id,
            "direction": 1,
        },
        timestamp=_TS,
        reason="test-b-close",
    )
    close_res = await closer.process_signal_execution(close_signal)
    assert close_res is not None
    assert len(close_res.orders) == 2
    by_symbol = {o.symbol: o for o in close_res.orders}
    assert by_symbol["XLE"].side == OrderSide.SELL
    assert by_symbol["XOP"].side == OrderSide.BUY

    async with db_factory() as session:
        row = await PositionRepository(session).get_by_trade_id(trade_id)
        assert row is not None
        assert row.risk_state == "CLOSED"
        assert row.closed_at is not None


@pytest.mark.asyncio
async def test_c_duplicate_signal_survives_restart(db_factory: async_sessionmaker[AsyncSession]) -> None:
    """TEST C: PROCESSED OPEN is still a duplicate after a new OrderManager instance."""
    trade_id = f"MBG-PERSIST-C-{uuid4().hex[:8]}"

    async with db_factory() as session, session.begin():
        account = await _seed_allocation(session)
        await SignalRepository(session).record_processed(
            Signal(
                signal_type=SignalType.BUY,
                timestamp=_TS,
                reason="pre-persisted",
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                action="OPEN",
                trade_id=trade_id,
                direction=1,
                legs=(
                    SignalLeg("XLE", "STK", 0.5943, Decimal("62.59")),
                    SignalLeg("XOP", "STK", -0.4057, Decimal("183.34")),
                ),
            ),
            persist_signal_id=trade_id,
        )

    oms = _oms()
    manager = _order_manager(db_factory, oms, account_id=account.id, account=account)
    await manager.hydrate_runtime_from_db()
    payload = {**XLE_XOP_OPEN, "trade_id": trade_id}
    with pytest.raises(ValueError, match="DUPLICATE_SIGNAL"):
        await manager.process_signal_execution(
            parse_model_blue_payload(payload, timestamp=_TS, reason="test-c")
        )


@pytest.mark.asyncio
async def test_d_allocation_is_authoritative_for_sizer(db_factory: async_sessionmaker[AsyncSession]) -> None:
    """TEST D: committed capital comes from PostgreSQL, not an env fallback."""
    async with db_factory() as session, session.begin():
        account = await _seed_allocation(session, committed=_COMMITTED)

    provider = DatabaseCommittedCapitalProvider(db_factory, account_id=account.id)
    committed = await provider.get_committed(MODEL_BLUE_STRATEGY_ID)
    assert committed == _COMMITTED

    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(committed))
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="test-d",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        action="OPEN",
        trade_id="MBG-ALLOC-D",
        direction=1,
        legs=(
            SignalLeg("XLE", "STK", 0.5943, Decimal("62.59")),
            SignalLeg("XOP", "STK", -0.4057, Decimal("183.34")),
        ),
    )
    xle, _xop = sizer.size_open(signal)
    expected_qty = (_COMMITTED / Decimal("62.59")).quantize(Decimal("0.0001"))
    assert xle.quantity == expected_qty

    missing = DatabaseCommittedCapitalProvider(db_factory, account_id=account.id + 10_000_000)
    assert await missing.get_committed(MODEL_BLUE_STRATEGY_ID) is None


@pytest.mark.asyncio
async def test_allocation_repository_uses_margin_times_pct(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session, session.begin():
        account = await _seed_allocation(session, committed=Decimal(40000))
        committed = await AllocationRepository(session).get_committed_notional(
            MODEL_BLUE_STRATEGY_ID, account_id=account.id
        )
        assert committed == Decimal(40000)
