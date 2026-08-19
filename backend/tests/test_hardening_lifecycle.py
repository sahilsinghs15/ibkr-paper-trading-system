"""Hardening regressions: quantity, RMS, persist, P&L, CLOSE. No live Gateway."""

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
from app.db.models import AccountModel, AllocationModel, StrategyModel
from app.db.models.event import EventLogModel
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.repositories.position_repository import PositionRepository
from app.db.session import create_engine_from_settings
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.oms_service import OMSService
from app.rms import RMSContext, RMSEngine
from app.rms.engine import get_default_checks
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSOutcome,
    StrategyConfig,
)
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.model_blue.db_trade_book import DatabaseModelBlueTradeBook
from app.services.model_blue.parser import (
    MODEL_BLUE_STRATEGY_ID,
    parse_model_blue_payload,
)
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.model_blue.sizer import ModelBlueSizer
from app.services.order_manager import OrderManager
from app.services.pnl import LivePnlService, unrealized_leg, unrealized_pair
from tests.ibkr_test_utils import fill_on_place_order

_TS = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
_XLE = Decimal("62.59")
_XOP = Decimal("183.34")


def _open_payload(trade_id: str) -> dict:
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
    account_id: int, ibkr: str, *, total: Decimal, pct: Decimal, max_open: int = 10
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
    tws.next_order_id = 400
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    fill_on_place_order(adapter, tws)
    return OMSService(adapter=adapter)


def test_default_rms_checks_are_duplicate_strategy_month_limit_money() -> None:
    names = [c.check_name for c in get_default_checks()]
    assert names == [
        "DUPLICATE",
        "STRATEGY",
        "CONTRACT_MONTH",
        "OPEN_POSITION_LIMIT",
        "MONEY_PER_STOCK",
    ]


def test_rms_evaluates_floored_quantity() -> None:
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(Decimal(25000)))
    signal = parse_model_blue_payload(_open_payload("T-QTY"), timestamp=_TS, reason="qty")
    xle, xop = sizer.size_open(signal)
    intent = OrderIntent(
        signal_id="T-QTY",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        action=OrderAction.OPEN,
        account_id=7,
        ibkr_account="DU-TEST",
        legs=[
            OrderLeg(
                symbol=xle.symbol,
                side=xle.side,
                quantity=float(xle.quantity),
                price=xle.price,
                contract_month="2026-09",
                notional=xle.notional,
                instrument_type="STK",
                leg_index=0,
            ),
            OrderLeg(
                symbol=xop.symbol,
                side=xop.side,
                quantity=float(xop.quantity),
                price=xop.price,
                contract_month="2026-09",
                notional=xop.notional,
                instrument_type="STK",
                leg_index=1,
            ),
        ],
        timestamp=_TS,
    )
    result = RMSEngine().evaluate(
        intent,
        RMSContext(
            strategy_configs={
                MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    max_open_positions=10,
                    money_limit_per_symbol=Decimal(10_000_000),
                )
            }
        ),
    )
    assert result.outcome == RMSOutcome.PASS
    assert [leg.quantity for leg in result.intent.legs] == [399.0, 93.0]
    assert len(result.check_results) == 5


def test_unrealized_pnl_long_and_short() -> None:
    long_pnl = unrealized_leg(Decimal(399), Decimal("62.59"), Decimal("63.59"))
    short_pnl = unrealized_leg(Decimal(-93), Decimal("183.34"), Decimal("182.34"))
    assert long_pnl == Decimal(399)
    assert short_pnl == Decimal(93)
    assert unrealized_pair(
        leg_a_signed=Decimal(399),
        leg_a_entry=Decimal("62.59"),
        leg_a_mark=Decimal("63.59"),
        leg_b_signed=Decimal(-93),
        leg_b_entry=Decimal("183.34"),
        leg_b_mark=Decimal("182.34"),
    ) == Decimal(492)


@pytest.mark.asyncio
async def test_live_pnl_persists_marks_not_entry() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-PNL-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"pnl-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            account_id = account.id
            session.add(
                PositionModel(
                    account_id=account_id,
                    trade_id=trade_id,
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    leg_a_symbol="XLE",
                    leg_a_signed_qty=Decimal(399),
                    leg_a_entry_mark=Decimal("62.59"),
                    leg_b_symbol="XOP",
                    leg_b_signed_qty=Decimal(-93),
                    leg_b_entry_mark=Decimal("183.34"),
                    target=Decimal(500),
                    stop=Decimal(250),
                    time_limit=3600,
                    risk_state="OPEN",
                )
            )

        client = MagicMock()
        client.reqMktData = MagicMock()
        svc = LivePnlService(factory, client)
        svc._loop = None
        intent = OrderIntent(
            signal_id=trade_id,
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            action=OrderAction.OPEN,
            account_id=account_id,
            legs=[
                OrderLeg(
                    symbol="XLE",
                    side=OrderSide.BUY,
                    quantity=399,
                    price=Decimal("62.59"),
                    contract_month="2026-09",
                    instrument_type="STK",
                ),
                OrderLeg(
                    symbol="XOP",
                    side=OrderSide.SELL,
                    quantity=93,
                    price=Decimal("183.34"),
                    contract_month="2026-09",
                    instrument_type="STK",
                ),
            ],
            timestamp=_TS,
        )
        svc.watch_open(intent)
        assert svc._legs[(account_id, trade_id)]["XLE"][1] == Decimal("62.59")
        xle_req = next(rid for rid, mapped in svc._by_req.items() if mapped[2] == "XLE")
        xop_req = next(rid for rid, mapped in svc._by_req.items() if mapped[2] == "XOP")
        svc.on_tick_price(xle_req, 4, 63.59)
        svc.on_tick_price(xop_req, 68, 182.34)
        await svc._persist(account_id, trade_id, Decimal(492))
        async with factory() as session:
            row = await PositionRepository(session).get_by_trade_id(
                trade_id, account_id=account_id
            )
            assert row is not None
            assert row.live_pnl == Decimal(492)
            assert row.leg_a_entry_mark == Decimal("62.59")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_pnl_hydrates_open_stk_and_skips_unresolved_cfd() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_stk = f"T-PNL-HY-{uuid4().hex[:8]}"
    trade_cfd = f"T-PNL-CFD-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"pnlh-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            account_id = account.id
            session.add(
                PositionModel(
                    account_id=account_id,
                    trade_id=trade_stk,
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    leg_a_symbol="XLE",
                    leg_a_signed_qty=Decimal(399),
                    leg_a_entry_mark=Decimal("62.59"),
                    leg_b_symbol="XOP",
                    leg_b_signed_qty=Decimal(-93),
                    leg_b_entry_mark=Decimal("183.34"),
                    leg_a_instrument_type="STK",
                    leg_b_instrument_type="STK",
                    target=Decimal(500),
                    stop=Decimal(250),
                    time_limit=3600,
                    risk_state="OPEN",
                )
            )
            session.add(
                PositionModel(
                    account_id=account_id,
                    trade_id=trade_cfd,
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    leg_a_symbol="ZZZCFDA",
                    leg_a_signed_qty=Decimal(10),
                    leg_a_entry_mark=Decimal("90.64"),
                    leg_b_symbol="ZZZCFDB",
                    leg_b_signed_qty=Decimal(-10),
                    leg_b_entry_mark=Decimal("91.86"),
                    leg_a_instrument_type="CFD",
                    leg_b_instrument_type="CFD",
                    target=Decimal(500),
                    stop=Decimal(250),
                    time_limit=3600,
                    risk_state="OPEN",
                )
            )

        client = MagicMock()
        client.reqMktData = MagicMock()
        svc = LivePnlService(factory, client)
        manager = OrderManager(
            oms=_oms(),
            session_factory=factory,
        )
        manager._live_pnl = svc
        await manager.hydrate_live_pnl()
        assert (account_id, trade_stk) in svc._legs
        stk_reqs = [rid for rid, mapped in svc._by_req.items() if mapped[1] == trade_stk]
        assert len(stk_reqs) == 2
        stk_contracts = [
            c.args[1]
            for c in client.reqMktData.call_args_list
            if c.args[1].symbol in ("XLE", "XOP") and c.args[0] in stk_reqs
        ]
        assert len(stk_contracts) == 2
        assert all(c.secType == "STK" for c in stk_contracts)
        before = client.reqMktData.call_count
        await manager.hydrate_live_pnl()
        assert client.reqMktData.call_count == before
        assert len([rid for rid, mapped in svc._by_req.items() if mapped[1] == trade_stk]) == 2
        assert (account_id, trade_cfd) in svc._legs
        cfd_reqs = [rid for rid, mapped in svc._by_req.items() if mapped[1] == trade_cfd]
        assert cfd_reqs == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_close_uses_open_fill_qty_and_realized_pnl() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-CLOSE-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            strategy = (
                await session.execute(
                    select(StrategyModel).where(
                        StrategyModel.strategy_id == MODEL_BLUE_STRATEGY_ID
                    )
                )
            ).scalar_one_or_none()
            if strategy is None:
                session.add(
                    StrategyModel(
                        strategy_id=MODEL_BLUE_STRATEGY_ID,
                        legs=2,
                        expression="CFD",
                        max_open_positions=10,
                        weight_source="payload",
                        enabled=True,
                    )
                )
            account = AccountModel(
                name=f"close-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            session.add(
                AllocationModel(
                    account_id=account.id,
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    alloc_pct=Decimal("0.25"),
                    target=Decimal(500),
                    stop=Decimal(250),
                    time_limit=3600,
                    max_open_positions=10,
                    enabled=True,
                )
            )
            account_id = account.id
            ibkr = account.ibkr_account

        ctx = _ctx(account_id, ibkr, total=Decimal(100000), pct=Decimal("0.25"))
        manager = OrderManager(
            oms=_oms(),
            order_type="MARKET",
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            rms_engine=RMSEngine(),
            rms_context=RMSContext(
                strategy_configs={
                    MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                        strategy_id=MODEL_BLUE_STRATEGY_ID,
                        max_open_positions=10,
                        money_limit_per_symbol=Decimal(10_000_000),
                    )
                }
            ),
            account_router=StaticStrategyAccountRouter([ctx]),
            session_factory=factory,
            persistence=ModelBlueExecutionPersistence(factory),
            model_blue_trade_book=DatabaseModelBlueTradeBook(factory),
        )
        open_res = await manager.process_signal_execution(
            parse_model_blue_payload(_open_payload(trade_id), timestamp=_TS, reason="open")
        )
        assert open_res is not None and open_res.success
        by_sym = {o.symbol: o for o in open_res.orders if not o.is_compensation}
        assert by_sym["XLE"].quantity == 399.0
        assert by_sym["XOP"].quantity == 93.0
        assert by_sym["XLE"].filled_quantity == 399.0
        assert by_sym["XLE"].quantity == by_sym["XLE"].filled_quantity

        async with factory() as session:
            pos = await PositionRepository(session).get_by_trade_id(
                trade_id, account_id=account_id
            )
            assert pos is not None
            assert pos.leg_a_signed_qty == Decimal(399)
            assert pos.leg_a_entry_mark == _XLE
            events = (await session.execute(select(EventLogModel.kind))).scalars().all()
            assert "RMS_PASS" in events
            assert "BASKET_OPEN" in events
            assert "POSITION_OPEN" in events
            orders = (
                await session.execute(select(OrderModel).where(OrderModel.trade_id == trade_id))
            ).scalars().all()
            assert {o.fill_qty for o in orders if not o.is_compensation} == {
                Decimal("399.0000"),
                Decimal("93.0000"),
            }
            assert all(o.quantity == o.fill_qty for o in orders if not o.is_compensation)

        close_res = await manager.process_signal_execution(
            parse_model_blue_payload(
                {
                    "market": "SMART",
                    "strategy": "model_blue",
                    "action": "CLOSE",
                    "trade_id": trade_id,
                    "direction": 1,
                },
                timestamp=_TS,
                reason="close",
            )
        )
        assert close_res is not None and close_res.success
        close_by = {
            o.symbol: o
            for o in close_res.orders
            if o.intent.action == OrderAction.CLOSE and not o.is_compensation
        }
        assert close_by["XLE"].quantity == 399.0
        assert close_by["XOP"].quantity == 93.0
        assert close_by["XLE"].side == OrderSide.SELL
        assert close_by["XOP"].side == OrderSide.BUY

        async with factory() as session:
            pos = await PositionRepository(session).get_by_trade_id(
                trade_id, account_id=account_id
            )
            assert pos is not None
            assert pos.risk_state == "CLOSED"
            assert pos.realised_pnl == Decimal(0)
            assert pos.commission == Decimal(0)
            kinds = (await session.execute(select(EventLogModel.kind))).scalars().all()
            assert "POSITION_CLOSE" in kinds
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_realized_pnl_long_short_and_optional_commission() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-REAL-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"real-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            account_id = account.id
            await PositionRepository(session).open_trade(
                OpenModelBlueTrade(
                    trade_id=trade_id,
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    direction=1,
                    legs=(
                        OpenModelBlueTradeLeg(
                            symbol="XLE",
                            instrument_type="STK",
                            side=OrderSide.BUY,
                            quantity=Decimal(399),
                            price=Decimal("62.59"),
                        ),
                        OpenModelBlueTradeLeg(
                            symbol="XOP",
                            instrument_type="STK",
                            side=OrderSide.SELL,
                            quantity=Decimal(93),
                            price=Decimal("183.34"),
                        ),
                    ),
                ),
                account_id=account_id,
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
            )
            closed = await PositionRepository(session).close_trade(
                trade_id,
                account_id=account_id,
                exit_marks={"XLE": Decimal("63.59"), "XOP": Decimal("182.34")},
                commission=Decimal("1.25"),
            )
            assert closed.realised_pnl == Decimal(492) - Decimal("1.25")
            assert closed.commission == Decimal("1.25")
            assert closed.risk_state == "CLOSED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_account_a_limit_reject_does_not_stop_account_b() -> None:
    a = _ctx(81, "DU-TEST-A", total=Decimal(100000), pct=Decimal("0.25"), max_open=1)
    b = _ctx(82, "DU-TEST-B", total=Decimal(100000), pct=Decimal("0.25"), max_open=10)
    rms = RMSContext(
        strategy_configs={
            MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                max_open_positions=1,
                money_limit_per_symbol=Decimal(10_000_000),
            )
        },
        open_positions={(81, MODEL_BLUE_STRATEGY_ID): 1},
    )
    manager = OrderManager(
        oms=_oms(),
        order_type="MARKET",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        rms_engine=RMSEngine(),
        rms_context=rms,
        account_router=StaticStrategyAccountRouter([a, b]),
    )
    result = await manager.process_signal_execution(
        parse_model_blue_payload(_open_payload("MBG-ISO-1"), timestamp=_TS, reason="iso")
    )
    assert result is not None
    by_id = {o.account_id: o for o in result.outcomes}
    assert by_id[81].success is False
    assert by_id[81].error is not None and "OPEN_POSITION_LIMIT_REACHED" in by_id[81].error
    assert by_id[82].success is True
    assert all(o.intent.account_id == 82 for o in result.orders)


@pytest.mark.asyncio
async def test_zero_quantity_rejected_before_oms() -> None:
    intent = OrderIntent(
        signal_id="ZERO",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        action=OrderAction.OPEN,
        account_id=1,
        legs=[
            OrderLeg(
                symbol="XLE",
                side=OrderSide.BUY,
                quantity=0,
                price=_XLE,
                contract_month="2026-09",
            )
        ],
        timestamp=_TS,
    )
    from app.services.model_blue.strategy import ModelBlueStrategy

    manager = OrderManager(
        oms=_oms(),
        rms_engine=RMSEngine(),
        rms_context=RMSContext(
            strategy_configs={
                MODEL_BLUE_STRATEGY_ID: StrategyConfig(
                    strategy_id=MODEL_BLUE_STRATEGY_ID,
                    max_open_positions=10,
                    money_limit_per_symbol=Decimal(10_000_000),
                )
            }
        ),
    )
    with pytest.raises(ValueError, match="ZERO_QUANTITY"):
        await manager._evaluate_and_submit(
            intent,
            parse_model_blue_payload(_open_payload("ZERO"), timestamp=_TS, reason="z"),
            handler=ModelBlueStrategy(),
        )
