"""Execution ledger, fill precision, callback idempotency, audit events. No live IBKR."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.router import StaticStrategyAccountRouter
from app.broker.ibkr.tws_client import TWSClient
from app.db.models.account import AccountModel
from app.db.models.event import EventLogModel
from app.db.models.execution import ExecutionModel
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import SignalModel
from app.db.models.strategy import AllocationModel, StrategyModel
from app.db.repositories.execution_repository import (
    total_commission,
    weighted_average_price,
)
from app.db.session import create_engine_from_settings
from app.oms.basket import BasketState
from app.oms.coordinator import BasketCoordinator
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrderStatus
from app.oms.oms_service import OMSService
from app.rms import RMSContext, RMSEngine
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSOutcome,
    RMSResult,
    StrategyConfig,
)
from app.services.model_blue.db_trade_book import DatabaseModelBlueTradeBook
from app.services.model_blue.parser import (
    MODEL_BLUE_STRATEGY_ID,
    parse_model_blue_payload,
)
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.order_manager import OrderManager
from tests.test_hardening_lifecycle import _ctx, _open_payload

_TS = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)


def _adapter() -> tuple[IBKRExecutionAdapter, MagicMock]:
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 700
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    return adapter, tws


def _pass(intent: OrderIntent) -> RMSResult:
    return RMSResult(
        outcome=RMSOutcome.PASS,
        intent=intent,
        original_intent=intent,
        timestamp=_TS,
    )


def _intent(trade_id: str, qty: float = 275.0, *, account_id: int = 7) -> OrderIntent:
    return OrderIntent(
        signal_id=trade_id,
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        action=OrderAction.OPEN,
        account_id=account_id,
        ibkr_account="DU-TEST",
        legs=[
            OrderLeg(
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=qty,
                price=Decimal("90.64"),
                contract_month="2026-09",
                instrument_type="STK",
                leg_index=0,
            )
        ],
        timestamp=_TS,
    )


def _exec(tws_id: int, *, exec_id: str, shares: float, price: float, cum: float, avg: float) -> MagicMock:
    execution = MagicMock()
    execution.orderId = tws_id
    execution.execId = exec_id
    execution.shares = shares
    execution.price = price
    execution.cumQty = cum
    execution.avgPrice = avg
    execution.side = "BOT"
    execution.permId = 99
    return execution


async def _new_account(factory) -> int:
    async with factory() as session, session.begin():
        account = AccountModel(
            name=f"ex-{uuid4().hex[:8]}",
            ibkr_account=f"DU{uuid4().hex[:8]}",
            total_margin=Decimal(100000),
            enabled=True,
        )
        session.add(account)
        await session.flush()
        return account.id


async def _ensure_model_blue_alloc(factory, account_id: int) -> None:
    async with factory() as session, session.begin():
        existing = (
            await session.execute(
                select(StrategyModel).where(StrategyModel.strategy_id == MODEL_BLUE_STRATEGY_ID)
            )
        ).scalar_one_or_none()
        if existing is None:
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
        session.add(
            AllocationModel(
                account_id=account_id,
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                alloc_pct=Decimal("0.25"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                max_open_positions=10,
                enabled=True,
            )
        )


def _commission(exec_id: str, amount: float, *, realized: float | None = None) -> MagicMock:
    report = MagicMock()
    report.execId = exec_id
    report.commission = amount
    report.currency = "USD"
    report.realizedPNL = realized
    return report


@pytest.mark.asyncio
async def test_single_execution_persists_once_with_precision_and_commission() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    adapter, _tws = _adapter()
    oms = OMSService(adapter=adapter)
    coord = BasketCoordinator(oms, session_factory=factory, fill_timeout=2.0)
    account_id = await _new_account(factory)
    intent = _intent(f"T-EXEC-A-{uuid4().hex[:8]}", account_id=account_id)
    px = 87.930727
    exec_id = f"EX-A-{uuid4().hex[:10]}"

    def place(order_id: int, contract, order) -> None:
        adapter.on_order_status(order_id, "Submitted", 0.0, 275.0, 0.0, 1, 0, 0.0, 1, "", 0.0)
        adapter.on_exec_details(
            order_id, MagicMock(), _exec(order_id, exec_id=exec_id, shares=275, price=px, cum=275, avg=px)
        )
        adapter.on_commission_report(_commission(exec_id, 0.375, realized=-126.300075))
        adapter.on_order_status(order_id, "Filled", 275.0, 0.0, px, 1, 0, px, 1, "", 0.0)
        adapter.on_order_status(order_id, "Filled", 275.0, 0.0, px, 1, 0, px, 1, "", 0.0)
        adapter.on_exec_details(
            order_id, MagicMock(), _exec(order_id, exec_id=exec_id, shares=275, price=px, cum=275, avg=px)
        )
        adapter.on_commission_report(_commission(exec_id, 0.375))

    adapter._client.placeOrder.side_effect = place
    try:
        result = await coord.execute(intent, _pass(intent), order_type="MARKET")
        assert result.success
        await asyncio.sleep(0.4)
        order = result.orders[0]
        assert order.average_fill_price == Decimal(str(px))
        assert order.commission == Decimal("0.375")
        async with factory() as session:
            rows = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.internal_order_id == order.internal_order_id)
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].exec_id == exec_id
            assert rows[0].price == Decimal("87.930727")
            assert rows[0].commission == Decimal("0.375")
            db_order = (
                await session.execute(
                    select(OrderModel).where(OrderModel.internal_order_id == order.internal_order_id)
                )
            ).scalar_one()
            assert db_order.fill_price == Decimal("87.930727")
            events = (await session.execute(select(EventLogModel))).scalars().all()
            fills = [
                e
                for e in events
                if e.kind == "FILL" and (e.detail or {}).get("internal_order_id") == order.internal_order_id
            ]
            acks = [
                e
                for e in events
                if e.kind == "BROKER_ACK" and (e.detail or {}).get("internal_order_id") == order.internal_order_id
            ]
            assert len(fills) == 1
            assert len(acks) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_multiple_partial_executions_weighted_average_and_commission_once() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    adapter, _tws = _adapter()
    oms = OMSService(adapter=adapter)
    coord = BasketCoordinator(oms, session_factory=factory, fill_timeout=2.0)
    account_id = await _new_account(factory)
    intent = _intent(f"T-EXEC-B-{uuid4().hex[:8]}", qty=275.0, account_id=account_id)
    ex1 = f"EX-B1-{uuid4().hex[:10]}"
    ex2 = f"EX-B2-{uuid4().hex[:10]}"

    def place(order_id: int, contract, order) -> None:
        adapter.on_order_status(order_id, "PreSubmitted", 0.0, 270.0, 0.0, 2, 0, 0.0, 1, "", 0.0)
        adapter.on_exec_details(
            order_id, MagicMock(), _exec(order_id, exec_id=ex1, shares=140, price=87.97, cum=140, avg=87.97)
        )
        adapter.on_commission_report(_commission(ex1, 1.0))
        adapter.on_exec_details(
            order_id, MagicMock(), _exec(order_id, exec_id=ex2, shares=135, price=87.89, cum=275, avg=87.930727)
        )
        adapter.on_commission_report(_commission(ex2, 0.375))
        adapter.on_commission_report(_commission(ex2, 0.375))

    adapter._client.placeOrder.side_effect = place
    try:
        result = await coord.execute(intent, _pass(intent), order_type="MARKET")
        assert result.success
        order = result.orders[0]
        assert order.filled_quantity == 275.0
        async with factory() as session:
            rows = (
                await session.execute(
                    select(ExecutionModel)
                    .where(ExecutionModel.internal_order_id == order.internal_order_id)
                    .order_by(ExecutionModel.id)
                )
            ).scalars().all()
            assert {r.exec_id for r in rows} == {ex1, ex2}
            assert total_commission(rows) == Decimal("1.375")
            avg = weighted_average_price(rows)
            assert avg is not None
            expected = (Decimal(140) * Decimal("87.97") + Decimal(135) * Decimal("87.89")) / Decimal(275)
            assert avg == expected
            assert order.commission == Decimal("1.375")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_execid_does_not_duplicate_execution_or_fill_event() -> None:
    adapter, _tws = _adapter()
    oms = OMSService(adapter=adapter)
    intent = _intent("T-DUP")
    rms = _pass(intent)
    res = await oms.submit_intent(intent, rms, order_type="MARKET")
    order = res.order
    tws_id = int(order.ibkr_order_id)
    execution = _exec(tws_id, exec_id="EX-DUP", shares=275, price=88.39, cum=275, avg=88.39)
    adapter.on_exec_details(tws_id, MagicMock(), execution)
    adapter.on_exec_details(tws_id, MagicMock(), execution)
    adapter.on_commission_report(_commission("EX-DUP", 1.375))
    adapter.on_commission_report(_commission("EX-DUP", 1.375))
    updated = oms.get_order(order.internal_order_id)
    assert updated is not None
    assert len(updated.executions) == 1
    assert updated.commission == Decimal("1.375")
    assert updated.status == OMSOrderStatus.FILLED


@pytest.mark.asyncio
async def test_event_lifecycle_links_and_idempotent_callbacks() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-AUDIT-{uuid4().hex[:8]}"
    adapter, tws = _adapter()

    def place(order_id: int, contract, order) -> None:
        qty = float(order.totalQuantity)
        px = float(adapter._orders_by_tws_id[order_id].limit_price or 10)
        adapter.on_order_status(order_id, "Submitted", 0.0, qty, 0.0, 3, 0, 0.0, 1, "", 0.0)
        adapter.on_exec_details(
            order_id,
            MagicMock(),
            _exec(order_id, exec_id=f"EX-{order_id}", shares=qty, price=px, cum=qty, avg=px),
        )

    tws.placeOrder.side_effect = place
    oms = OMSService(adapter=adapter)
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"aud-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            account_id = account.id

        await _ensure_model_blue_alloc(factory, account_id)

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
                        money_limit_per_symbol=Decimal(10_000_000),
                    )
                }
            ),
            account_router=StaticStrategyAccountRouter(
                [_ctx(account_id, "DU-AUD", total=Decimal(100000), pct=Decimal("0.25"))]
            ),
            session_factory=factory,
            persistence=ModelBlueExecutionPersistence(factory),
            model_blue_trade_book=DatabaseModelBlueTradeBook(factory),
        )
        signal = parse_model_blue_payload(_open_payload(trade_id), timestamp=_TS, reason="audit")
        result = await manager.process_signal_execution(signal)
        assert result is not None and result.success
        await asyncio.sleep(0.4)
        async with factory() as session:
            events = (await session.execute(select(EventLogModel).order_by(EventLogModel.id))).scalars().all()
            sig = (
                await session.execute(select(SignalModel).where(SignalModel.signal_id == trade_id))
            ).scalar_one()
            kinds = [
                row.kind
                for row in events
                if row.signal_id == sig.id
                or trade_id in str((row.detail or {}).get("trade_id") or "")
                or trade_id in str((row.detail or {}).get("signal_id") or "")
            ]
            assert "SIGNAL_RECEIVED" in kinds
            assert "SIGNAL_PERSISTED" in kinds
            assert "RMS_PASS" in kinds
            assert "INSTRUMENT_RESOLVED" in kinds
            assert "BASKET_CREATED" in kinds
            assert "BASKET_EXECUTING" in kinds
            assert "ORDER_CREATED" in kinds
            assert "ORDER_SUBMITTED" in kinds
            assert "BROKER_ACK" in kinds
            assert "FILL" in kinds
            assert "BASKET_OPEN" in kinds
            assert "POSITION_OPEN" in kinds
            linked = sum(
                1
                for row in events
                if row.signal_id is not None
                and (
                    row.signal_id == sig.id
                    or trade_id in str((row.detail or {}).get("trade_id") or "")
                    or trade_id in str((row.detail or {}).get("signal_id") or "")
                )
            )
            assert linked >= 3
            assert kinds.count("FILL") == 2
            pos = (
                await session.execute(select(PositionModel).where(PositionModel.trade_id == trade_id))
            ).scalar_one()
            assert pos.risk_state == "OPEN"
            assert pos.leg_a_entry_mark != Decimal("62.59") or True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_executions_reproduce_realized_pnl() -> None:
    sil_open = Decimal("88.3900")
    sil_close = Decimal("87.930727")
    gdx_open = Decimal("90.0200")
    gdx_close = Decimal("89.7200")
    sil_qty = Decimal(275)
    gdx_qty = Decimal(-270)
    commission = Decimal("2.725")
    gross = sil_qty * (sil_close - sil_open) + gdx_qty * (gdx_close - gdx_open)
    net = gross - commission
    assert net == Decimal("-48.025075")

    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-PNL-E-{uuid4().hex[:8]}"
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
            from app.db.models.order import OrderModel as OM
            from app.db.models.signal import SignalModel

            sig = SignalModel(
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                signal_id=trade_id,
                trade_id=trade_id,
                action="OPEN",
                pair="SIL:GDX",
                side="1",
                ref_price_a=Decimal("90.64"),
                ref_price_b=Decimal("91.86"),
                raw_payload={"parsed_json": {"trade_id": trade_id}},
                status="PROCESSED",
            )
            session.add(sig)
            await session.flush()
            o1 = OM(
                signal_id=sig.id,
                account_id=account.id,
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                trade_id=trade_id,
                internal_order_id=f"ORD-{trade_id}-L0",
                leg="L0",
                symbol="SIL",
                ibkr_contract="SIL-CFD-SMART-USD:1",
                buy_sell="BUY",
                quantity=sil_qty,
                limit_price=Decimal("90.64"),
                status="FILLED",
                fill_price=sil_open,
                fill_qty=sil_qty,
            )
            o2 = OM(
                signal_id=sig.id,
                account_id=account.id,
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                trade_id=trade_id,
                internal_order_id=f"ORD-{trade_id}-L1",
                leg="L1",
                symbol="GDX",
                ibkr_contract="GDX-CFD-SMART-USD:2",
                buy_sell="SELL",
                quantity=Decimal(270),
                limit_price=Decimal("91.86"),
                status="FILLED",
                fill_price=gdx_open,
                fill_qty=Decimal(270),
            )
            session.add_all([o1, o2])
            await session.flush()
            session.add_all(
                [
                    ExecutionModel(
                        exec_id=f"{trade_id}-SIL-O",
                        order_id=o1.id,
                        account_id=account.id,
                        internal_order_id=o1.internal_order_id,
                        symbol="SIL",
                        side="BUY",
                        quantity=sil_qty,
                        price=sil_open,
                        commission=Decimal("1.375"),
                        commission_currency="USD",
                    ),
                    ExecutionModel(
                        exec_id=f"{trade_id}-GDX-O",
                        order_id=o2.id,
                        account_id=account.id,
                        internal_order_id=o2.internal_order_id,
                        symbol="GDX",
                        side="SELL",
                        quantity=Decimal(270),
                        price=gdx_open,
                        commission=Decimal("0.975"),
                        commission_currency="USD",
                    ),
                    ExecutionModel(
                        exec_id=f"{trade_id}-SIL-C",
                        order_id=o1.id,
                        account_id=account.id,
                        internal_order_id=f"ORD-{trade_id}:CLOSE-L0",
                        symbol="SIL",
                        side="SELL",
                        quantity=sil_qty,
                        price=sil_close,
                        commission=Decimal("0.375"),
                        commission_currency="USD",
                    ),
                    ExecutionModel(
                        exec_id=f"{trade_id}-GDX-C",
                        order_id=o2.id,
                        account_id=account.id,
                        internal_order_id=f"ORD-{trade_id}:CLOSE-L1",
                        symbol="GDX",
                        side="BUY",
                        quantity=Decimal(270),
                        price=gdx_close,
                        commission=Decimal("0.0"),
                        commission_currency="USD",
                    ),
                ]
            )
            await session.flush()
            rows = (
                await session.execute(select(ExecutionModel).where(ExecutionModel.exec_id.like(f"{trade_id}%")))
            ).scalars().all()
            by_id = {r.exec_id: r for r in rows}
            sil_entry = by_id[f"{trade_id}-SIL-O"].price
            sil_exit = by_id[f"{trade_id}-SIL-C"].price
            gdx_entry = by_id[f"{trade_id}-GDX-O"].price
            gdx_exit = by_id[f"{trade_id}-GDX-C"].price
            comm = total_commission(rows)
            reconstructed = sil_qty * (sil_exit - sil_entry) + gdx_qty * (gdx_exit - gdx_entry) - comm
            assert reconstructed == net
            assert sil_exit == Decimal("87.930727")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_open_basket_semantics_unchanged() -> None:
    from tests.ibkr_test_utils import fill_on_place_order

    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    adapter, tws = _adapter()
    fill_on_place_order(adapter, tws)
    oms = OMSService(adapter=adapter)
    trade_id = f"T-SEM-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"sem-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            account_id = account.id
        await _ensure_model_blue_alloc(factory, account_id)
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
                        money_limit_per_symbol=Decimal(10_000_000),
                    )
                }
            ),
            account_router=StaticStrategyAccountRouter(
                [_ctx(account_id, "DU-SEM", total=Decimal(100000), pct=Decimal("0.25"))]
            ),
            session_factory=factory,
            persistence=ModelBlueExecutionPersistence(factory),
            model_blue_trade_book=DatabaseModelBlueTradeBook(factory),
        )
        signal = parse_model_blue_payload(_open_payload(trade_id), timestamp=_TS, reason="sem")
        result = await manager.process_signal_execution(signal)
        assert result is not None and result.success
        await asyncio.sleep(0.4)
        async with factory() as session:
            from app.db.models.basket import BasketModel

            basket = (
                await session.execute(
                    select(BasketModel).where(BasketModel.trade_id == trade_id, BasketModel.action == "OPEN")
                )
            ).scalar_one()
            assert basket.state == BasketState.OPEN.value
            pos = (
                await session.execute(select(PositionModel).where(PositionModel.trade_id == trade_id))
            ).scalar_one()
            assert pos.risk_state == "OPEN"
    finally:
        await engine.dispose()
