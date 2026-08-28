"""Production-path lifecycle gaps. Mocked IBKR only — no live Gateway."""

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
    PositionModel,
    SignalModel,
    StrategyModel,
)
from app.db.repositories.order_repository import OrderRepository
from app.db.session import create_engine_from_settings
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrder, OMSOrderStatus
from app.oms.oms_service import OMSService
from app.rms import RMSContext, RMSEngine
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    StrategyConfig,
)
from app.services.model_blue.parser import (
    MODEL_BLUE_STRATEGY_ID,
    parse_model_blue_payload,
)
from app.services.order_manager import OrderManager
from tests.ibkr_test_utils import fill_on_place_order, wire_test_managed_accounts

_TS = datetime(2026, 8, 18, 17, 40, tzinfo=UTC)


def _open_payload(trade_id: str, *, itype: str = "STK") -> dict:
    return {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": trade_id,
        "direction": 1,
        "buckets": [
            {
                "underlying": "SIL",
                "legs": [
                    {"instrument_type": itype, "side": "BUY", "weight": 0.5019, "price": 90.64}
                ],
            },
            {
                "underlying": "GDX",
                "legs": [
                    {
                        "instrument_type": itype,
                        "side": "SELL",
                        "weight": -0.4981,
                        "price": 91.86,
                    }
                ],
            },
        ],
    }


def _ctx(account_id: int, ibkr: str) -> AccountExecutionContext:
    return AccountExecutionContext(
        account_id=account_id,
        ibkr_account=ibkr,
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        total_margin=Decimal(100000),
        alloc_pct=Decimal("0.25"),
        committed_notional=Decimal(25000),
        target=Decimal(500),
        stop=Decimal(250),
        time_limit=3600,
        max_open_positions=10,
    )


def _oms(*managed_accounts: str) -> tuple[OMSService, MagicMock]:
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 800
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    if managed_accounts:
        adapter.set_managed_accounts(list(managed_accounts))
    else:
        wire_test_managed_accounts(adapter)
    fill_on_place_order(adapter, tws)
    return OMSService(adapter=adapter), tws


@pytest.mark.asyncio
async def test_rms_reject_does_not_place_order() -> None:
    oms, tws = _oms()
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
                    money_limit_per_symbol=Decimal(1),
                )
            }
        ),
        account_router=StaticStrategyAccountRouter([_ctx(1, "DU-A")]),
    )
    with pytest.raises(ValueError, match="MONEY_LIMIT_EXCEEDED"):
        await manager.process_signal_execution(
            parse_model_blue_payload(_open_payload("T-RMS"), timestamp=_TS, reason="rms")
        )
    tws.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_cfd_signal_rejects_before_placeorder_without_master() -> None:
    oms, tws = _oms()
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
        account_router=StaticStrategyAccountRouter([_ctx(1, "DU-A")]),
    )
    with pytest.raises(ValueError, match="INSTRUMENT_METADATA_MISSING|INSTRUMENT_RESOLUTION_FAILED"):
        await manager.process_signal_execution(
            parse_model_blue_payload(
                _open_payload("T-CFD-MISS", itype="CFD"), timestamp=_TS, reason="cfd"
            )
        )
    tws.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_etf_signal_reaches_ibkr_as_stk() -> None:
    oms, tws = _oms("DU-ETF")
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
        account_router=StaticStrategyAccountRouter([_ctx(3, "DU-ETF")]),
    )
    result = await manager.process_signal_execution(
        parse_model_blue_payload(_open_payload("T-ETF", itype="ETF"), timestamp=_TS, reason="etf")
    )
    assert result is not None and result.success
    contracts = [call.args[1] for call in tws.placeOrder.call_args_list]
    assert contracts
    assert all(c.secType == "STK" for c in contracts)
    for order in result.orders:
        assert order.resolved is not None
        assert order.resolved.requested_instrument_type == "ETF"
        assert order.resolved.sec_type == "STK"
        assert order.quantity == int(order.quantity)


@pytest.mark.asyncio
async def test_stk_paper_quantities_are_integers_on_adapter() -> None:
    oms, tws = _oms("DU-STK")
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
        account_router=StaticStrategyAccountRouter([_ctx(4, "DU-STK")]),
    )
    result = await manager.process_signal_execution(
        parse_model_blue_payload(_open_payload("T-STK-INT"), timestamp=_TS, reason="stk")
    )
    assert result is not None and result.success
    ib_qtys = [call.args[2].totalQuantity for call in tws.placeOrder.call_args_list]
    assert ib_qtys
    assert all(float(q) == int(q) for q in ib_qtys)
    by_sym = {o.symbol: o for o in result.orders if not o.is_compensation}
    assert by_sym["SIL"].quantity == int(by_sym["SIL"].quantity)
    assert by_sym["GDX"].quantity == int(by_sym["GDX"].quantity)


@pytest.mark.asyncio
async def test_callback_persist_idempotent_does_not_regress_filled() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    oid = f"ORD-IDEM-{uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            account = AccountModel(
                name=f"idem-{uuid4().hex[:8]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(account)
            await session.flush()
            sig = SignalModel(
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                signal_id=f"SIG-{uuid4().hex[:8]}",
                action="OPEN",
                pair="SIL:GDX",
                side="1",
                ref_price_a=Decimal("90.64"),
                raw_payload={},
                status="NEW",
            )
            session.add(sig)
            await session.flush()
            intent = OrderIntent(
                signal_id="T-IDEM",
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                action=OrderAction.OPEN,
                account_id=account.id,
                legs=[
                    OrderLeg(
                        symbol="SIL",
                        side=OrderSide.BUY,
                        quantity=10,
                        price=Decimal("90.64"),
                        contract_month="2026-09",
                        instrument_type="STK",
                    )
                ],
                timestamp=_TS,
            )
            filled = OMSOrder(
                internal_order_id=oid,
                intent=intent,
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=10,
                ibkr_order_id=9001,
                status=OMSOrderStatus.FILLED,
                filled_quantity=10,
                remaining_quantity=0,
                average_fill_price=Decimal("90.64"),
                limit_price=Decimal("90.64"),
                parent_signal_id="T-IDEM",
                leg_index=0,
            )
            repo = OrderRepository(session)
            await repo.record_oms_order(
                filled,
                signal_pk=sig.id,
                account_id=account.id,
                trade_id="T-IDEM",
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                leg_label="L0",
            )
            late = OMSOrder(
                internal_order_id=oid,
                intent=intent,
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=10,
                ibkr_order_id=9001,
                status=OMSOrderStatus.SUBMITTED,
                filled_quantity=0,
                remaining_quantity=10,
                limit_price=Decimal("90.64"),
                parent_signal_id="T-IDEM",
                leg_index=0,
            )
            await repo.record_oms_order(
                late,
                signal_pk=sig.id,
                account_id=account.id,
                trade_id="T-IDEM",
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                leg_label="L0",
            )
            row = await repo.get_by_internal_id(oid)
            assert row is not None
            assert row.status == OMSOrderStatus.FILLED.value
            assert float(row.fill_qty) == 10.0
            await repo.record_oms_order(
                filled,
                signal_pk=sig.id,
                account_id=account.id,
                trade_id="T-IDEM",
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                leg_label="L0",
            )
            again = await repo.get_by_internal_id(oid)
            assert again is not None
            assert again.status == OMSOrderStatus.FILLED.value
            count = (
                await session.execute(select(OrderModel).where(OrderModel.internal_order_id == oid))
            ).scalars().all()
            assert len(count) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_trade_id_two_accounts_two_positions() -> None:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-MULTI-{uuid4().hex[:8]}"
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
            accounts = []
            for name in ("A", "B"):
                acct = AccountModel(
                    name=f"multi-{name}-{uuid4().hex[:8]}",
                    ibkr_account=f"DU{name}{uuid4().hex[:6]}",
                    total_margin=Decimal(100000),
                    enabled=True,
                )
                session.add(acct)
                await session.flush()
                session.add(
                    AllocationModel(
                        account_id=acct.id,
                        strategy_id=MODEL_BLUE_STRATEGY_ID,
                        alloc_pct=Decimal("0.25"),
                        target=Decimal(500),
                        stop=Decimal(250),
                        time_limit=3600,
                        max_open_positions=10,
                        enabled=True,
                    )
                )
                accounts.append(acct)
            a_id, b_id = accounts[0].id, accounts[1].id
            a_ibkr, b_ibkr = accounts[0].ibkr_account, accounts[1].ibkr_account

        from app.services.model_blue.db_trade_book import DatabaseModelBlueTradeBook
        from app.services.model_blue.persistence import ModelBlueExecutionPersistence

        oms, _tws = _oms(a_ibkr, b_ibkr)
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
                [_ctx(a_id, a_ibkr), _ctx(b_id, b_ibkr)]
            ),
            session_factory=factory,
            persistence=ModelBlueExecutionPersistence(factory),
            model_blue_trade_book=DatabaseModelBlueTradeBook(factory),
        )
        result = await manager.process_signal_execution(
            parse_model_blue_payload(_open_payload(trade_id), timestamp=_TS, reason="multi")
        )
        assert result is not None
        assert result.success
        assert {o.account_id for o in result.outcomes} == {a_id, b_id}
        async with factory() as session:
            rows = (
                await session.execute(
                    select(PositionModel).where(PositionModel.trade_id == trade_id)
                )
            ).scalars().all()
            assert {r.account_id for r in rows} == {a_id, b_id}
            assert all(r.risk_state == "OPEN" for r in rows)
            assert all(r.leg_a_instrument_type == "STK" for r in rows)
    finally:
        await engine.dispose()
