"""Multi-account strategy routing: Signal -> Strategy -> eligible Accounts."""

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.config_service import (
    AccountStrategyConfigService,
    AllocationConfigError,
)
from app.accounts.context import AccountExecutionContext
from app.accounts.router import (
    DatabaseStrategyAccountRouter,
    StaticStrategyAccountRouter,
)
from app.broker.ibkr.tws_client import TWSClient
from app.db.models import AccountModel, AllocationModel, StrategyModel
from app.db.session import create_engine_from_settings
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.oms_service import OMSService
from app.rms import RMSContext, RMSEngine
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide, StrategyConfig
from app.services.model_blue.db_trade_book import DatabaseModelBlueTradeBook
from app.services.model_blue.parser import (
    MODEL_BLUE_STRATEGY_ID,
    parse_model_blue_payload,
)
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.order_manager import OrderManager
from app.services.strategies.inbound import parse_tradingview_payload

_TS = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
_XLE = Decimal("62.59")
_XOP = Decimal("183.34")


def _open_payload(trade_id: str) -> dict[str, Any]:
    return {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": trade_id,
        "direction": 1,
        "buckets": [
            {
                "underlying": "XLE",
                "legs": [
                    {"instrument_type": "STK", "side": "BUY", "weight": 0.5943, "price": float(_XLE)}
                ],
            },
            {
                "underlying": "XOP",
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "SELL",
                        "weight": -0.4057,
                        "price": float(_XOP),
                    }
                ],
            },
        ],
    }


def _ctx(
    account_id: int,
    ibkr: str,
    *,
    total: Decimal,
    pct: Decimal,
    max_open: int = 10,
) -> AccountExecutionContext:
    return AccountExecutionContext(
        account_id=account_id,
        ibkr_account=ibkr,
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        total_margin=total,
        alloc_pct=pct,
        committed_notional=total * pct,
        target=Decimal(500),
        stop=Decimal(250),
        time_limit=3600,
        max_open_positions=max_open,
    )


def _oms() -> OMSService:
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 300
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    from tests.ibkr_test_utils import fill_on_place_order

    fill_on_place_order(adapter, tws)
    return OMSService(adapter=adapter)


def _manager(contexts: list[AccountExecutionContext], *, rms: RMSContext | None = None) -> OrderManager:
    return OrderManager(
        oms=_oms(),
        order_type="MARKET",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        rms_engine=RMSEngine(),
        rms_context=rms
        or RMSContext(
            strategy_configs={
                MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    max_open_positions=10,
                    money_limit_per_symbol=Decimal(1_000_000),
                )
            }
        ),
        account_router=StaticStrategyAccountRouter(contexts),
    )


@pytest.mark.asyncio
async def test_1_one_account_one_strategy_routes() -> None:
    ctx = _ctx(11, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    manager = _manager([ctx])
    signal = parse_model_blue_payload(_open_payload("MBG-ACC-1"), timestamp=_TS, reason="t1")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    assert len(result.outcomes) == 1
    assert result.outcomes[0].account_id == 11
    assert result.outcomes[0].ibkr_account == "DU-TEST-A"
    assert len(result.orders) == 2
    assert {o.intent.account_id for o in result.orders} == {11}
    assert {o.symbol for o in result.orders} == {"XLE", "XOP"}


@pytest.mark.asyncio
async def test_2_signal_fans_out_to_two_enabled_accounts() -> None:
    a = _ctx(21, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    b = _ctx(22, "DU-TEST-B", total=Decimal(50000), pct=Decimal("0.40"))
    manager = _manager([a, b])
    signal = parse_model_blue_payload(_open_payload("MBG-ACC-2"), timestamp=_TS, reason="t2")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    assert {o.account_id for o in result.outcomes} == {21, 22}
    assert len(result.orders) == 4


@pytest.mark.asyncio
async def test_3_disabled_account_not_in_static_router() -> None:
    enabled = _ctx(31, "DU-TEST-ON", total=Decimal(100000), pct=Decimal("0.25"))
    manager = _manager([enabled])
    signal = parse_model_blue_payload(_open_payload("MBG-ACC-3"), timestamp=_TS, reason="t3")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    assert [o.account_id for o in result.outcomes] == [31]


@pytest.mark.asyncio
async def test_6_and_7_independent_sizing_different_alloc_pct() -> None:
    a = _ctx(41, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    b = _ctx(42, "DU-TEST-B", total=Decimal(50000), pct=Decimal("0.40"))
    manager = _manager([a, b])
    signal = parse_model_blue_payload(_open_payload("MBG-ACC-67"), timestamp=_TS, reason="t67")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    by_acct: dict[int, list] = {}
    for order in result.orders:
        assert order.intent.account_id is not None
        by_acct.setdefault(order.intent.account_id, []).append(order)
    xle_a = next(o for o in by_acct[41] if o.symbol == "XLE")
    xle_b = next(o for o in by_acct[42] if o.symbol == "XLE")
    assert xle_a.quantity != xle_b.quantity
    expected_a = float((Decimal(25000) / _XLE).quantize(Decimal(1), rounding=ROUND_DOWN))
    expected_b = float((Decimal(20000) / _XLE).quantize(Decimal(1), rounding=ROUND_DOWN))
    assert xle_a.quantity == pytest.approx(expected_a)
    assert xle_b.quantity == pytest.approx(expected_b)


@pytest.mark.asyncio
async def test_8_account_a_rms_pass_account_b_reject() -> None:
    a = _ctx(51, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    b = _ctx(52, "DU-TEST-B", total=Decimal(50000), pct=Decimal("0.40"))
    rms = RMSContext(
        strategy_configs={
            MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                max_open_positions=10,
                money_limit_per_symbol=Decimal(1_000_000),
            )
        },
        processed_signals={(52, MODEL_BLUE_STRATEGY_ID, "MBG-ACC-8")},
    )
    manager = _manager([a, b], rms=rms)
    signal = parse_model_blue_payload(_open_payload("MBG-ACC-8"), timestamp=_TS, reason="t8")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    by_id = {o.account_id: o for o in result.outcomes}
    assert by_id[51].success is True
    assert by_id[52].success is False
    assert by_id[52].error is not None and "DUPLICATE_SIGNAL" in by_id[52].error
    assert all(o.intent.account_id == 51 for o in result.orders)


@pytest.mark.asyncio
async def test_9_per_symbol_limits_are_account_specific() -> None:
    a = _ctx(61, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    b = _ctx(62, "DU-TEST-B", total=Decimal(100000), pct=Decimal("0.25"))
    rms = RMSContext(
        strategy_configs={
            MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                max_open_positions=10,
                money_limit_per_symbol=None,
            )
        },
        per_symbol_limits={
            (61, "XLE"): Decimal(50000),
            (62, "XLE"): Decimal(100),
        },
    )
    manager = _manager([a, b], rms=rms)
    signal = parse_model_blue_payload(_open_payload("MBG-ACC-9"), timestamp=_TS, reason="t9")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    by_id = {o.account_id: o for o in result.outcomes}
    assert by_id[61].success is True
    assert by_id[62].success is False
    assert by_id[62].error is not None and "MONEY_LIMIT_EXCEEDED" in by_id[62].error


@pytest.mark.asyncio
async def test_10_orders_carry_account_id() -> None:
    ctx = _ctx(71, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    result = await _manager([ctx]).process_signal_execution(
        parse_model_blue_payload(_open_payload("MBG-ACC-10"), timestamp=_TS, reason="t10")
    )
    assert result is not None
    for order in result.orders:
        assert order.intent.account_id == 71
        assert order.intent.ibkr_account == "DU-TEST-A"
        assert "71-" in order.internal_order_id


@pytest.mark.asyncio
async def test_12_same_symbol_independent_intents() -> None:
    a = _ctx(81, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    b = _ctx(82, "DU-TEST-B", total=Decimal(50000), pct=Decimal("0.40"))
    result = await _manager([a, b]).process_signal_execution(
        parse_model_blue_payload(_open_payload("MBG-ACC-12"), timestamp=_TS, reason="t12")
    )
    assert result is not None
    xle = [o for o in result.orders if o.symbol == "XLE"]
    assert len(xle) == 2
    assert {o.intent.account_id for o in xle} == {81, 82}


@pytest.mark.asyncio
async def test_13_model_blue_remains_two_leg() -> None:
    ctx = _ctx(91, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    result = await _manager([ctx]).process_signal_execution(
        parse_model_blue_payload(_open_payload("MBG-ACC-13"), timestamp=_TS, reason="t13")
    )
    assert result is not None
    assert len(result.orders) == 2


@pytest.mark.asyncio
async def test_17_unknown_strategy_does_not_route_to_model_blue() -> None:
    ctx = _ctx(101, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"))
    manager = _manager([ctx])
    inbound = parse_tradingview_payload(
        {"strategy": "model_red", "symbol": "SPY", "quantity": 1, "price": 100, "action": "OPEN"},
        timestamp=_TS,
        request_id="r17",
        capture_data={},
    )
    assert inbound.strategy_id == "model_red"
    with pytest.raises(ValueError, match="UNKNOWN_STRATEGY"):
        await manager.process_signal_execution(inbound)


@pytest.mark.asyncio
async def test_18_no_eligible_accounts_does_not_pick_default() -> None:
    manager = OrderManager(
        oms=_oms(),
        order_type="MARKET",
        account_router=StaticStrategyAccountRouter([]),
        rms_context=RMSContext(
            strategy_configs={
                MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    max_open_positions=10,
                    money_limit_per_symbol=Decimal(1_000_000),
                )
            }
        ),
    )
    signal = parse_model_blue_payload(_open_payload("MBG-ACC-18"), timestamp=_TS, reason="t18")
    with pytest.raises(ValueError, match="NO_ELIGIBLE_ACCOUNTS"):
        await manager.process_signal_execution(signal)


def test_generic_n_leg_intent_still_iterates_all_legs() -> None:
    intent = OrderIntent(
        signal_id="N3",
        strategy_id="synthetic_n_leg",
        action=OrderAction.OPEN,
        account_id=5,
        ibkr_account="DU-TEST-N",
        legs=[
            OrderLeg(
                symbol=sym,
                side=OrderSide.BUY,
                quantity=2,
                price=Decimal(10),
                contract_month="2026-09",
                notional=Decimal(20),
                leg_index=i,
            )
            for i, sym in enumerate(["L0", "L1", "L2"])
        ],
        timestamp=_TS,
    )
    context = RMSContext(
        strategy_configs={
            "synthetic_n_leg": StrategyConfig(
                strategy_id="synthetic_n_leg",
                max_open_positions=10,
                money_limit_per_symbol=Decimal(1_000_000),
            )
        }
    )
    result = RMSEngine().evaluate(intent, context)
    assert result.outcome.value == "PASS"
    assert len(result.intent.legs) == 3


@pytest.fixture
async def db_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_db_router_respects_enabled_flags(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:8]
    strategy_id = MODEL_BLUE_STRATEGY_ID
    async with db_factory() as session, session.begin():
        strategy = (
            await session.execute(
                select(StrategyModel).where(StrategyModel.strategy_id == strategy_id)
            )
        ).scalar_one_or_none()
        if strategy is None:
            session.add(
                StrategyModel(
                    strategy_id=strategy_id,
                    legs=2,
                    expression="STK",
                    max_open_positions=10,
                    weight_source="payload",
                    enabled=True,
                )
            )
            await session.flush()
        on_acct = AccountModel(
            name=f"iso-on-{suffix}",
            ibkr_account=f"DUON{suffix}",
            total_margin=Decimal(100000),
            enabled=True,
        )
        off_acct = AccountModel(
            name=f"iso-off-{suffix}",
            ibkr_account=f"DUOFF{suffix}",
            total_margin=Decimal(100000),
            enabled=False,
        )
        session.add_all([on_acct, off_acct])
        await session.flush()
        alloc_on = AllocationModel(
            account_id=on_acct.id,
            strategy_id=strategy_id,
            alloc_pct=Decimal("0.10"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
            max_open_positions=10,
            enabled=True,
        )
        alloc_off_acct = AllocationModel(
            account_id=off_acct.id,
            strategy_id=strategy_id,
            alloc_pct=Decimal("0.10"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
            max_open_positions=10,
            enabled=True,
        )
        disabled_sub = AccountModel(
            name=f"iso-suboff-{suffix}",
            ibkr_account=f"DUSUB{suffix}",
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add(disabled_sub)
        await session.flush()
        alloc_sub_off = AllocationModel(
            account_id=disabled_sub.id,
            strategy_id=strategy_id,
            alloc_pct=Decimal("0.10"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
            max_open_positions=10,
            enabled=False,
        )
        session.add_all([alloc_on, alloc_off_acct, alloc_sub_off])
        on_id, off_id, sub_id = on_acct.id, off_acct.id, disabled_sub.id

    router = DatabaseStrategyAccountRouter(db_factory)
    resolved = await router.resolve(strategy_id)
    ids = {ctx.account_id for ctx in resolved}
    assert on_id in ids
    assert off_id not in ids
    assert sub_id not in ids

    async with db_factory() as session, session.begin():
        for row in (
            await session.execute(
                select(AllocationModel).where(
                    AllocationModel.account_id.in_([on_id, off_id, sub_id])
                )
            )
        ).scalars():
            await session.delete(row)
        for row in (
            await session.execute(
                select(AccountModel).where(AccountModel.id.in_([on_id, off_id, sub_id]))
            )
        ).scalars():
            await session.delete(row)


@pytest.mark.asyncio
async def test_config_service_rejects_alloc_sum_over_one(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:8]
    async with db_factory() as session, session.begin():
        strategy = (
            await session.execute(
                select(StrategyModel).where(StrategyModel.strategy_id == MODEL_BLUE_STRATEGY_ID)
            )
        ).scalar_one_or_none()
        if strategy is None:
            session.add(
                StrategyModel(
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    legs=2,
                    expression="STK",
                    max_open_positions=10,
                    weight_source="payload",
                    enabled=True,
                )
            )
        green_id = f"model_green_{suffix}"
        session.add(
            StrategyModel(
                strategy_id=green_id,
                legs=1,
                expression="STK",
                max_open_positions=10,
                weight_source="payload",
                enabled=True,
            )
        )
        account = AccountModel(
            name=f"iso-sum-{suffix}",
            ibkr_account=f"DUSUM{suffix}",
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add(account)
        await session.flush()
        svc = AccountStrategyConfigService(session)
        await svc.create_allocation(
            account=account,
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            alloc_pct=Decimal("0.80"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
        )
        with pytest.raises(AllocationConfigError, match="ALLOC_PCT_SUM_EXCEEDED"):
            await svc.create_allocation(
                account=account,
                strategy_id=green_id,
                alloc_pct=Decimal("0.30"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
            )


@pytest.mark.asyncio
async def test_11_positions_isolated_by_account_id(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.db.repositories.position_repository import PositionRepository

    suffix = uuid4().hex[:8]
    trade_id = f"MBG-POS-{suffix}"
    async with db_factory() as session, session.begin():
        strategy = (
            await session.execute(
                select(StrategyModel).where(StrategyModel.strategy_id == MODEL_BLUE_STRATEGY_ID)
            )
        ).scalar_one_or_none()
        if strategy is None:
            session.add(
                StrategyModel(
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    legs=2,
                    expression="STK",
                    max_open_positions=10,
                    weight_source="payload",
                    enabled=True,
                )
            )
            await session.flush()
        acct_a = AccountModel(
            name=f"iso-pa-{suffix}",
            ibkr_account=f"DUPA{suffix}",
            total_margin=Decimal(100000),
            enabled=True,
        )
        acct_b = AccountModel(
            name=f"iso-pb-{suffix}",
            ibkr_account=f"DUPB{suffix}",
            total_margin=Decimal(50000),
            enabled=True,
        )
        session.add_all([acct_a, acct_b])
        await session.flush()
        for acct, pct in ((acct_a, Decimal("0.25")), (acct_b, Decimal("0.40"))):
            session.add(
                AllocationModel(
                    account_id=acct.id,
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    alloc_pct=pct,
                    target=Decimal(500),
                    stop=Decimal(250),
                    time_limit=3600,
                    max_open_positions=10,
                    enabled=True,
                )
            )
        a_id, b_id = acct_a.id, acct_b.id
        ibkr_a, ibkr_b = acct_a.ibkr_account, acct_b.ibkr_account
        margin_a, margin_b = acct_a.total_margin, acct_b.total_margin

    contexts = [
        _ctx(a_id, ibkr_a, total=margin_a, pct=Decimal("0.25")),
        _ctx(b_id, ibkr_b, total=margin_b, pct=Decimal("0.40")),
    ]
    oms = _oms()
    manager = OrderManager(
        oms=oms,
        order_type="MARKET",
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
        account_router=StaticStrategyAccountRouter(contexts),
        session_factory=db_factory,
        persistence=ModelBlueExecutionPersistence(db_factory),
        model_blue_trade_book=DatabaseModelBlueTradeBook(db_factory),
    )
    signal = parse_model_blue_payload(_open_payload(trade_id), timestamp=_TS, reason="t11")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    assert result.success is True

    async with db_factory() as session:
        pos = PositionRepository(session)
        row_a = await pos.get_by_trade_id(trade_id, account_id=a_id)
        row_b = await pos.get_by_trade_id(trade_id, account_id=b_id)
        assert row_a is not None and row_b is not None
        assert row_a.account_id == a_id
        assert row_b.account_id == b_id
        assert row_a.leg_a_symbol == "XLE"
        with pytest.raises(ValueError, match="AMBIGUOUS_TRADE_ID"):
            await pos.get_by_trade_id(trade_id)

    async with db_factory() as session, session.begin():
        from app.db.models.basket import BasketModel
        from app.db.models.event import EventLogModel
        from app.db.models.order import OrderModel
        from app.db.models.position import PositionModel
        from app.db.models.signal import SignalModel

        signal_ids = [
            row.id
            for row in (
                await session.execute(select(SignalModel).where(SignalModel.signal_id == trade_id))
            ).scalars()
        ]
        if signal_ids:
            for row in (
                await session.execute(
                    select(EventLogModel).where(EventLogModel.signal_id.in_(signal_ids))
                )
            ).scalars():
                await session.delete(row)
        for row in (
            await session.execute(select(OrderModel).where(OrderModel.trade_id == trade_id))
        ).scalars():
            await session.delete(row)
        for row in (
            await session.execute(select(BasketModel).where(BasketModel.trade_id == trade_id))
        ).scalars():
            await session.delete(row)
        for row in (
            await session.execute(
                select(PositionModel).where(PositionModel.trade_id == trade_id)
            )
        ).scalars():
            await session.delete(row)
        for row in (
            await session.execute(select(SignalModel).where(SignalModel.signal_id == trade_id))
        ).scalars():
            await session.delete(row)


@pytest.mark.asyncio
async def test_router_uses_allocation_max_open_not_strategy(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Check 7 cap comes from allocations.max_open_positions (per account)."""
    suffix = uuid4().hex[:8]
    strategy_id = f"CAP_{suffix}"
    async with db_factory() as session, session.begin():
        strategy = StrategyModel(
            strategy_id=strategy_id,
            legs=2,
            expression="CFD",
            max_open_positions=99,
            weight_source="payload",
            enabled=True,
        )
        account = AccountModel(
            name=f"cap-{suffix}",
            ibkr_account=f"DUCAP{suffix}",
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add_all([strategy, account])
        await session.flush()
        session.add(
            AllocationModel(
                account_id=account.id,
                strategy_id=strategy_id,
                alloc_pct=Decimal("0.10"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                max_open_positions=4,
                enabled=True,
            )
        )
        account_id = account.id

    router = DatabaseStrategyAccountRouter(db_factory)
    contexts = await router.resolve(strategy_id)
    assert len(contexts) == 1
    assert contexts[0].max_open_positions == 4
    assert contexts[0].max_open_positions != 99

    async with db_factory() as session, session.begin():
        for row in (
            await session.execute(
                select(AllocationModel).where(AllocationModel.account_id == account_id)
            )
        ).scalars():
            await session.delete(row)
        for row in (
            await session.execute(select(AccountModel).where(AccountModel.id == account_id))
        ).scalars():
            await session.delete(row)
        for row in (
            await session.execute(
                select(StrategyModel).where(StrategyModel.strategy_id == strategy_id)
            )
        ).scalars():
            await session.delete(row)

