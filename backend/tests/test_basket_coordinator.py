"""BasketCoordinator atomicity tests. All IBKR interactions are mocked."""

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.tws_client import TWSClient
from app.db.models import AccountModel, BasketModel, OrderModel, PositionModel
from app.db.session import create_engine_from_settings
from app.models.signal import Signal, SignalType
from app.oms.basket import BasketState
from app.oms.coordinator import BasketCoordinator
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrderStatus
from app.oms.oms_service import OMSService
from app.rms.engine import RMSEngine
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    RMSResult,
    StrategyConfig,
)
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.model_blue.parser import (
    MODEL_BLUE_STRATEGY_ID,
    parse_model_blue_payload,
)
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.model_blue.sizer import ModelBlueSizer
from app.services.order_manager import OrderManager

_TS = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
_STRAT = "synthetic_n_leg"


def _pass(intent: OrderIntent) -> RMSResult:
    return RMSResult(
        outcome=RMSOutcome.PASS,
        intent=intent,
        original_intent=intent,
        timestamp=_TS,
    )


def _leg(symbol: str, index: int, qty: float = 100.0, side: OrderSide = OrderSide.BUY) -> OrderLeg:
    return OrderLeg(
        symbol=symbol,
        side=side,
        quantity=qty,
        price=Decimal(10),
        contract_month="2026-09",
        notional=Decimal(str(qty)) * Decimal(10),
        instrument_type="STK",
        leg_index=index,
    )


def _intent(
    symbols: list[str],
    *,
    trade_id: str,
    account_id: int = 1,
    qtys: list[float] | None = None,
    ibkr_account: str = "DUTEST",
) -> OrderIntent:
    qty = qtys or [100.0] * len(symbols)
    return OrderIntent(
        signal_id=trade_id,
        strategy_id=_STRAT,
        action=OrderAction.OPEN,
        account_id=account_id,
        ibkr_account=ibkr_account,
        legs=[_leg(sym, i, qty[i]) for i, sym in enumerate(symbols)],
        timestamp=_TS,
    )


class PlaceScript:
    """Deterministic mock broker: consume placeOrder steps, then fill remaining."""

    def __init__(self, steps: list[str]) -> None:
        self.steps = list(steps)
        self.cancel_ids: list[int] = []
        self.place_count = 0

    def bind(self, adapter: IBKRExecutionAdapter, client: MagicMock) -> None:
        def fake_place(order_id: int, contract: Any, order: Any) -> None:
            self.place_count += 1
            qty = float(getattr(order, "totalQuantity", 0) or 0)
            raw_px = float(getattr(order, "lmtPrice", 0) or 0)
            px = raw_px if math.isfinite(raw_px) and 0 < raw_px < 1e12 else 10.0
            step = self.steps.pop(0) if self.steps else "fill"
            if step == "fill":
                adapter.on_order_status(
                    order_id, "Filled", qty, 0.0, px, 0, 0, px, 1, "", 0.0
                )
            elif step == "reject":
                adapter.on_order_status(
                    order_id, "Inactive", 0.0, qty, 0.0, 0, 0, 0.0, 1, "", 0.0
                )
            elif step == "pending":
                adapter.on_order_status(
                    order_id, "Submitted", 0.0, qty, 0.0, 0, 0, 0.0, 1, "", 0.0
                )
            elif step.startswith("partial:"):
                filled = float(step.split(":", 1)[1])
                adapter.on_order_status(
                    order_id,
                    "Submitted",
                    filled,
                    max(0.0, qty - filled),
                    px,
                    0,
                    0,
                    px,
                    1,
                    "",
                    0.0,
                )
            elif step == "warn399_then_fill":
                adapter.on_error(
                    order_id,
                    399,
                    "Order Message: Warning: Your order will not be placed at the exchange until RTH.",
                )
                adapter.on_order_status(
                    order_id, "Filled", qty, 0.0, px, 0, 0, px, 1, "", 0.0
                )
            elif step == "error":
                raise RuntimeError("placeOrder failed")
            else:
                raise AssertionError(f"unknown script step {step}")

        def fake_cancel(order_id: int) -> None:
            self.cancel_ids.append(order_id)
            with adapter._lock:
                tracked = adapter._orders_by_tws_id.get(order_id)
            filled = float(tracked.filled_quantity) if tracked else 0.0
            remaining = max(0.0, (tracked.quantity if tracked else 0.0) - filled)
            px = float(tracked.average_fill_price or 0) if tracked and tracked.average_fill_price else 0.0
            adapter.on_order_status(
                order_id, "Cancelled", filled, remaining, px, 0, 0, px, 1, "", 0.0
            )

        client.placeOrder.side_effect = fake_place
        client.cancelOrder.side_effect = fake_cancel
        client.reqOpenOrders = MagicMock()
        client.reqExecutions = MagicMock()


def _wired(script: PlaceScript) -> tuple[OMSService, IBKRExecutionAdapter, PlaceScript]:
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 500
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    script.bind(adapter, tws)
    return OMSService(adapter=adapter), adapter, script


def _coord(oms: OMSService, **kwargs: Any) -> BasketCoordinator:
    return BasketCoordinator(oms, fill_timeout=0.2, cancel_timeout=0.2, **kwargs)


@pytest.mark.asyncio
async def test_two_leg_happy_path_open() -> None:
    oms, _adapter, script = _wired(PlaceScript(["fill", "fill"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-HAPPY")
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.OPEN
    assert result.success is True
    assert [o.status for o in result.orders] == [OMSOrderStatus.FILLED, OMSOrderStatus.FILLED]
    assert result.compensation_orders == []
    assert script.cancel_ids == []


@pytest.mark.asyncio
async def test_leg0_fill_leg1_reject_compensates_leg0() -> None:
    oms, _adapter, _script = _wired(PlaceScript(["fill", "reject"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-REJ1")
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert result.success is False
    assert result.orders[0].status == OMSOrderStatus.FILLED
    assert result.orders[1].status == OMSOrderStatus.REJECTED
    assert len(result.compensation_orders) == 1
    comp = result.compensation_orders[0]
    assert comp.symbol == "XLE"
    assert comp.side == OrderSide.SELL
    assert comp.quantity == pytest.approx(100.0)
    assert comp.is_compensation is True
    assert comp.compensation_of_internal_order_id == result.orders[0].internal_order_id


@pytest.mark.asyncio
async def test_leg0_reject_leg1_never_executes() -> None:
    oms, _adapter, script = _wired(PlaceScript(["reject"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-REJ0")
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert len(result.orders) == 1
    assert result.orders[0].status == OMSOrderStatus.REJECTED
    assert result.compensation_orders == []
    assert script.place_count == 1


@pytest.mark.asyncio
async def test_partial_fill_compensates_actual_qty_not_requested() -> None:
    oms, _adapter, script = _wired(PlaceScript(["partial:37", "reject"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-PART", qtys=[100.0, 100.0])
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert result.orders[0].filled_quantity == pytest.approx(37.0)
    assert len(result.compensation_orders) == 1
    assert result.compensation_orders[0].quantity == pytest.approx(37.0)
    assert result.compensation_orders[0].quantity != 100.0
    assert script.cancel_ids


@pytest.mark.asyncio
async def test_leg1_place_order_throws_compensates_leg0() -> None:
    oms, _adapter, _script = _wired(PlaceScript(["fill", "error"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-THROW")
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert result.orders[1].status == OMSOrderStatus.ERROR
    assert len(result.compensation_orders) == 1
    assert result.compensation_orders[0].symbol == "XLE"
    assert result.compensation_orders[0].quantity == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_pending_leg_cancelled_before_compensation() -> None:
    oms, _adapter, script = _wired(PlaceScript(["fill", "pending"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-PEND")
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert script.cancel_ids
    assert result.orders[1].status == OMSOrderStatus.CANCELLED
    assert len(result.compensation_orders) == 1
    assert result.compensation_orders[0].symbol == "XLE"


@pytest.mark.asyncio
async def test_compensation_failure_is_critical_and_blocks_open() -> None:
    oms, _adapter, _script = _wired(PlaceScript(["fill", "reject", "error"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-CRIT", account_id=42)
    coord = _coord(oms)
    result = await coord.execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.CRITICAL
    assert coord.is_open_blocked(42, _STRAT) is True
    assert coord.is_open_blocked(99, _STRAT) is False


@pytest.mark.asyncio
async def test_same_signal_two_accounts_independent_baskets() -> None:
    oms_a, _, _ = _wired(PlaceScript(["fill", "fill"]))
    oms_b, _, _ = _wired(PlaceScript(["fill", "reject"]))
    intent_a = _intent(["XLE", "XOP"], trade_id="T-SHARED", account_id=10, ibkr_account="DUA")
    intent_b = _intent(["XLE", "XOP"], trade_id="T-SHARED", account_id=20, ibkr_account="DUB")
    res_a = await _coord(oms_a).execute(intent_a, _pass(intent_a), order_type="MARKET")
    res_b = await _coord(oms_b).execute(intent_b, _pass(intent_b), order_type="MARKET")
    assert res_a.state == BasketState.OPEN
    assert res_b.state == BasketState.COMPENSATED
    assert [o.intent.account_id for o in res_a.orders] == [10, 10]
    assert [o.intent.account_id for o in res_b.orders] == [20, 20]
    assert res_b.compensation_orders[0].intent.account_id == 20
    assert res_b.compensation_orders[0].intent.ibkr_account == "DUB"


@pytest.mark.asyncio
async def test_three_leg_failed_leg_compensates_filled_legs() -> None:
    oms, _, _ = _wired(PlaceScript(["fill", "fill", "reject"]))
    intent = _intent(["A", "B", "C"], trade_id="T-3", qtys=[10, 20, 30])
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert len(result.orders) == 3
    comps = result.compensation_orders
    assert {c.symbol: c.quantity for c in comps} == {"A": 10.0, "B": 20.0}
    assert all(c.side == OrderSide.SELL for c in comps)


@pytest.mark.asyncio
async def test_five_leg_failed_leg_compensates_filled_legs() -> None:
    oms, _, _ = _wired(PlaceScript(["fill", "fill", "fill", "fill", "reject"]))
    intent = _intent(["A", "B", "C", "D", "E"], trade_id="T-5")
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert len(result.orders) == 5
    assert len(result.compensation_orders) == 4
    assert {c.symbol for c in result.compensation_orders} == {"A", "B", "C", "D"}


@pytest.mark.asyncio
async def test_order_manager_incomplete_basket_does_not_call_after_submit() -> None:
    oms, _, _ = _wired(PlaceScript(["fill", "reject"]))
    persist = MagicMock(spec=ModelBlueExecutionPersistence)
    persist.persist_open = AsyncMock()
    persist.persist_close = AsyncMock()
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(Decimal(25000)))
    manager = OrderManager(
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
        committed_capital_provider=TemporarySettingsCommittedCapitalProvider(Decimal(25000)),
        model_blue_sizer=sizer,
        persistence=persist,
    )
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": "T-NO-OPEN",
        "direction": 1,
        "buckets": [
            {
                "underlying": "XLE",
                "legs": [{"instrument_type": "STK", "side": "BUY", "weight": 0.5943, "price": 62.59}],
            },
            {
                "underlying": "XOP",
                "legs": [{"instrument_type": "STK", "side": "SELL", "weight": -0.4057, "price": 183.34}],
            },
        ],
    }
    signal = parse_model_blue_payload(payload, timestamp=_TS, reason="no-open")
    result = await manager.process_signal_execution(signal)
    assert result is not None
    assert result.success is False
    persist.persist_open.assert_not_called()


@pytest.mark.asyncio
async def test_restart_incomplete_basket_requires_reconciliation() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-RESTART-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"basket-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            session.add(
                BasketModel(
                    account_id=account.id,
                    trade_id=trade_id,
                    strategy_id=_STRAT,
                    action="OPEN",
                    state=BasketState.EXECUTING.value,
                    intended_leg_count=2,
                )
            )
            account_id = account.id

        tws = MagicMock(spec=TWSClient)
        tws.is_connected.return_value = False
        adapter = IBKRExecutionAdapter(client=tws)
        adapter.is_connected = lambda: False  # type: ignore[method-assign]
        oms = OMSService(adapter=adapter)
        coord = BasketCoordinator(oms, session_factory=factory, fill_timeout=0.2)
        await coord.recover_incomplete_baskets()
        assert coord.is_open_blocked(account_id, _STRAT) is True
        async with factory() as session:
            row = (
                await session.execute(
                    select(BasketModel).where(
                        BasketModel.account_id == account_id,
                        BasketModel.trade_id == trade_id,
                    )
                )
            ).scalar_one()
            assert row.state == BasketState.CRITICAL.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_fills_and_no_open_position_on_incomplete() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-FILL-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"basket-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            account_id = account.id
            ibkr = account.ibkr_account

        oms, _, _ = _wired(PlaceScript(["fill", "reject"]))
        intent = _intent(["XLE", "XOP"], trade_id=trade_id, account_id=account_id, ibkr_account=ibkr)
        coord = BasketCoordinator(
            oms, session_factory=factory, fill_timeout=0.2, cancel_timeout=0.2
        )
        result = await coord.execute(intent, _pass(intent), order_type="MARKET")
        assert result.state == BasketState.COMPENSATED

        async with factory() as session:
            orders = (
                await session.execute(select(OrderModel).where(OrderModel.trade_id == trade_id))
            ).scalars().all()
            assert orders
            filled = [o for o in orders if o.symbol == "XLE" and not o.is_compensation]
            comps = [o for o in orders if o.is_compensation]
            assert filled[0].fill_qty == Decimal("100.0000") or float(filled[0].fill_qty) == 100.0
            assert filled[0].status == OMSOrderStatus.FILLED.value
            assert filled[0].broker_order_id is not None
            assert comps
            assert float(comps[0].quantity) == 100.0
            pos = (
                await session.execute(
                    select(PositionModel).where(
                        PositionModel.account_id == account_id,
                        PositionModel.trade_id == trade_id,
                    )
                )
            ).scalar_one_or_none()
            assert pos is None
            basket = (
                await session.execute(
                    select(BasketModel).where(
                        BasketModel.account_id == account_id,
                        BasketModel.trade_id == trade_id,
                    )
                )
            ).scalar_one()
            assert basket.state == BasketState.COMPENSATED.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_critical_blocks_order_manager_open() -> None:
    oms, _, _ = _wired(PlaceScript(["fill", "fill"]))
    manager = OrderManager(
        oms=oms,
        order_type="MARKET",
        strategy_id=_STRAT,
        rms_engine=RMSEngine(),
        rms_context=RMSContext(
            strategy_configs={
                _STRAT: StrategyConfig(
                    strategy_id=_STRAT,
                    max_open_positions=10,
                    money_limit_per_symbol=Decimal(1_000_000),
                )
            }
        ),
    )
    assert manager._baskets is not None
    manager._baskets.mark_critical(7, _STRAT)
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="blocked",
        signal_id="T-BLOCK",
        strategy_id=_STRAT,
        action="OPEN",
        symbol="SPY",
        side="BUY",
        price=Decimal(100),
        quantity=1,
    )
    # process_signal_execution may not copy account_id onto intent for generic path.
    from app.rms.models import OrderIntent as OI

    intent = OI(
        signal_id="T-BLOCK",
        strategy_id=_STRAT,
        action=OrderAction.OPEN,
        account_id=7,
        legs=[_leg("SPY", 0, 1)],
        timestamp=_TS,
    )
    with pytest.raises(ValueError, match="BASKET_CRITICAL"):
        await manager._evaluate_and_submit(intent, signal, handler=None)


@pytest.mark.asyncio
async def test_warning_399_does_not_reject_or_compensate() -> None:
    oms, _adapter, _script = _wired(PlaceScript(["warn399_then_fill", "warn399_then_fill"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-399")
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.OPEN
    assert result.success is True
    assert result.compensation_orders == []
    assert [o.status for o in result.orders] == [OMSOrderStatus.FILLED, OMSOrderStatus.FILLED]


@pytest.mark.asyncio
async def test_both_legs_partial_compensates_actual_quantities() -> None:
    oms, _adapter, script = _wired(PlaceScript(["partial:40", "partial:25"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-BOTH-PART", qtys=[100.0, 100.0])
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert result.success is False
    by_sym = {c.symbol: c.quantity for c in result.compensation_orders}
    assert by_sym == {"XLE": 40.0, "XOP": 25.0}
    assert 100.0 not in by_sym.values()
    assert script.cancel_ids


@pytest.mark.asyncio
async def test_cancel_acknowledgement_failure_is_critical() -> None:
    oms, adapter, _script = _wired(PlaceScript(["fill", "pending"]))
    adapter._client.cancelOrder.side_effect = RuntimeError("cancel ack failed")
    intent = _intent(["XLE", "XOP"], trade_id="T-CANCEL-FAIL", account_id=11)
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.CRITICAL
    assert result.success is False
    assert oms._adapter._client.cancelOrder.side_effect is not None


@pytest.mark.asyncio
async def test_restart_unwinding_basket_is_critical() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-UNWIND-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"unwind-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            session.add(
                BasketModel(
                    account_id=account.id,
                    trade_id=trade_id,
                    strategy_id=_STRAT,
                    action="OPEN",
                    state=BasketState.UNWINDING.value,
                    intended_leg_count=2,
                )
            )
            account_id = account.id

        tws = MagicMock(spec=TWSClient)
        tws.is_connected.return_value = False
        adapter = IBKRExecutionAdapter(client=tws)
        adapter.is_connected = lambda: False  # type: ignore[method-assign]
        coord = BasketCoordinator(OMSService(adapter=adapter), session_factory=factory)
        await coord.recover_incomplete_baskets()
        assert coord.is_open_blocked(account_id, _STRAT) is True
        async with factory() as session:
            row = (
                await session.execute(
                    select(BasketModel).where(
                        BasketModel.account_id == account_id,
                        BasketModel.trade_id == trade_id,
                    )
                )
            ).scalar_one()
            assert row.state == BasketState.CRITICAL.value
    finally:
        await engine.dispose()
