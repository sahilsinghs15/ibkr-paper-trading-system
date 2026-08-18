"""Temporary paper demo: requested STK executes as IBKR CFD without a catalog."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.instruments.execution_override import STK_TO_CFD_DEMO, execution_instrument_type
from app.instruments.models import InstrumentRecord, InstrumentResolutionError
from app.instruments.resolver import (
    InMemoryInstrumentCatalog,
    ibkr_contract_from_resolved,
    resolve_leg,
)
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrder, OMSOrderStatus
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.model_blue.parser import parse_model_blue_payload
from app.services.model_blue.persistence import _open_trade_from_fills
from app.services.model_blue.sizer import ModelBlueSizer
from app.services.model_blue.strategy import ModelBlueStrategy
from app.services.model_blue.trade_book import InMemoryModelBlueTradeBook

_TS = datetime(2026, 8, 18, 17, 0, tzinfo=UTC)


def _open_stk_payload(trade_id: str = "T-STK-CFD-DEMO") -> dict:
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
                    {
                        "instrument_type": "STK",
                        "side": "BUY",
                        "weight": 0.6,
                        "price": 90.0,
                    }
                ],
            },
            {
                "underlying": "GDX",
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "SELL",
                        "weight": -0.4,
                        "price": 40.0,
                    }
                ],
            },
        ],
    }


def _demo_cfd(symbol: str) -> object:
    return resolve_leg(
        symbol=symbol,
        instrument_type="STK",
        apply_demo_override=True,
    )


def test_parser_preserves_requested_stk() -> None:
    signal = parse_model_blue_payload(
        _open_stk_payload(), timestamp=_TS, reason="demo", raw_payload=_open_stk_payload()
    )
    assert [leg.instrument_type for leg in signal.legs] == ["STK", "STK"]
    assert signal.raw_payload is not None
    assert signal.raw_payload["buckets"][0]["legs"][0]["instrument_type"] == "STK"


def test_stk_to_cfd_demo_converts_execution_type() -> None:
    exec_type, override = execution_instrument_type("STK", enabled=True)
    assert exec_type == "CFD"
    assert override == STK_TO_CFD_DEMO
    resolved = _demo_cfd("SIL")
    assert resolved.requested_instrument_type == "STK"
    assert resolved.sec_type == "CFD"
    assert resolved.con_id is None


def test_empty_instruments_table_does_not_reject_demo_cfd() -> None:
    resolved = resolve_leg(
        symbol="SIL",
        instrument_type="STK",
        catalog=InMemoryInstrumentCatalog(),
        apply_demo_override=True,
    )
    assert resolved.sec_type == "CFD"
    assert ibkr_contract_from_resolved(resolved).secType == "CFD"


@pytest.mark.parametrize("symbol", ["SIL", "GDX"])
def test_demo_cfd_ibkr_contract_from_symbol(symbol: str) -> None:
    resolved = _demo_cfd(symbol)
    contract = ibkr_contract_from_resolved(resolved)
    assert contract.symbol == symbol
    assert contract.secType == "CFD"
    assert contract.secType != "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"
    assert not contract.conId


@pytest.mark.parametrize("symbol", ["SIL", "GDX"])
def test_ibkr_adapter_builds_cfd_contract(symbol: str) -> None:
    adapter = IBKRExecutionAdapter(client=MagicMock())
    intent = OrderIntent(
        signal_id="T-ADAPTER-CFD",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal("90"),
                contract_month="2026-09",
                instrument_type="STK",
                resolved=_demo_cfd(symbol),
                leg_index=0,
            )
        ],
        timestamp=_TS,
    )
    order = OMSOrder(
        internal_order_id="o-cfd",
        intent=intent,
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=10,
        resolved=intent.legs[0].resolved,
    )
    contract = adapter._build_ibkr_contract(order)
    assert contract.symbol == symbol
    assert contract.secType == "CFD"
    assert contract.secType != "STK"


def test_stk_catalog_row_does_not_fall_back_to_stk() -> None:
    catalog = InMemoryInstrumentCatalog(
        [
            InstrumentRecord(
                symbol="SIL",
                sec_type="STK",
                trade_conid=211651690,
                market_data_conid=211651690,
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
            )
        ]
    )
    resolved = resolve_leg(
        symbol="SIL",
        instrument_type="STK",
        catalog=catalog,
        apply_demo_override=True,
    )
    assert resolved.sec_type == "CFD"
    assert ibkr_contract_from_resolved(resolved).secType != "STK"


def test_override_disabled_still_resolves_stk() -> None:
    resolved = resolve_leg(
        symbol="SIL",
        instrument_type="STK",
        apply_demo_override=False,
    )
    assert resolved.requested_instrument_type == "STK"
    assert resolved.sec_type == "STK"
    assert ibkr_contract_from_resolved(resolved).secType == "STK"


def test_etf_is_not_mapped_to_cfd() -> None:
    resolved = resolve_leg(
        symbol="SIL",
        instrument_type="ETF",
        apply_demo_override=True,
    )
    assert resolved.requested_instrument_type == "ETF"
    assert resolved.sec_type == "STK"


def test_explicit_cfd_without_demo_still_requires_catalog() -> None:
    with pytest.raises(InstrumentResolutionError, match="INSTRUMENT_METADATA_MISSING"):
        resolve_leg(symbol="SIL", instrument_type="CFD", apply_demo_override=False)


def test_attach_resolved_keeps_requested_stk_on_leg() -> None:
    from app.instruments.resolver import attach_resolved

    intent = OrderIntent(
        signal_id="T-KEEP-STK",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal("90"),
                contract_month="2026-09",
                instrument_type="STK",
                leg_index=0,
            )
        ],
        timestamp=_TS,
    )
    resolved_intent = attach_resolved(intent, apply_demo_override=True)
    assert resolved_intent.legs[0].instrument_type == "STK"
    assert resolved_intent.legs[0].resolved is not None
    assert resolved_intent.legs[0].resolved.sec_type == "CFD"


def test_open_persists_resolved_cfd_not_requested_stk() -> None:
    intent = OrderIntent(
        signal_id="T-OPEN-CFD",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal("90"),
                contract_month="2026-09",
                instrument_type="STK",
                resolved=_demo_cfd("SIL"),
                leg_index=0,
            ),
            OrderLeg(
                symbol="GDX",
                side=OrderSide.SELL,
                quantity=10,
                price=Decimal("40"),
                contract_month="2026-09",
                instrument_type="STK",
                resolved=_demo_cfd("GDX"),
                leg_index=1,
            ),
        ],
        timestamp=_TS,
    )
    orders = [
        OMSOrder(
            internal_order_id="o1",
            intent=intent,
            symbol="SIL",
            side=OrderSide.BUY,
            quantity=10,
            status=OMSOrderStatus.FILLED,
            filled_quantity=10,
            average_fill_price=Decimal("90"),
            leg_index=0,
            resolved=intent.legs[0].resolved,
        ),
        OMSOrder(
            internal_order_id="o2",
            intent=intent,
            symbol="GDX",
            side=OrderSide.SELL,
            quantity=10,
            status=OMSOrderStatus.FILLED,
            filled_quantity=10,
            average_fill_price=Decimal("40"),
            leg_index=1,
            resolved=intent.legs[1].resolved,
        ),
    ]
    persisted = _open_trade_from_fills(
        OpenModelBlueTrade(
            trade_id="T-OPEN-CFD",
            strategy_id="model_blue",
            direction=1,
            legs=(),
        ),
        orders,
    )
    assert [leg.instrument_type for leg in persisted.legs] == ["CFD", "CFD"]
    assert persisted.legs[0].symbol == "SIL"


@pytest.mark.asyncio
async def test_close_uses_persisted_cfd_without_catalog() -> None:
    book = InMemoryModelBlueTradeBook()
    await book.record_open(
        OpenModelBlueTrade(
            trade_id="T-CLOSE-CFD",
            strategy_id="model_blue",
            direction=1,
            legs=(
                OpenModelBlueTradeLeg(
                    symbol="SIL",
                    instrument_type="CFD",
                    side=OrderSide.BUY,
                    quantity=Decimal("10"),
                    price=Decimal("90"),
                ),
                OpenModelBlueTradeLeg(
                    symbol="GDX",
                    instrument_type="CFD",
                    side=OrderSide.SELL,
                    quantity=Decimal("10"),
                    price=Decimal("40"),
                ),
            ),
        )
    )
    strategy = ModelBlueStrategy(trade_book=book)
    signal = parse_model_blue_payload(
        {
            "market": "SMART",
            "strategy": "model_blue",
            "action": "CLOSE",
            "trade_id": "T-CLOSE-CFD",
            "direction": 1,
        },
        timestamp=_TS,
        reason="close",
    )
    intent = await strategy.build_intent(signal)
    assert [leg.instrument_type for leg in intent.legs] == ["CFD", "CFD"]
    for leg in intent.legs:
        resolved = resolve_leg(
            symbol=leg.symbol,
            instrument_type=leg.instrument_type,
            apply_demo_override=True,
        )
        contract = ibkr_contract_from_resolved(resolved)
        assert resolved.requested_instrument_type == "CFD"
        assert resolved.sec_type == "CFD"
        assert contract.secType == "CFD"
        assert contract.secType != "STK"


def test_model_blue_sizing_still_uses_requested_stk() -> None:
    signal = parse_model_blue_payload(_open_stk_payload(), timestamp=_TS, reason="size")
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(Decimal("25000")))
    sized_base, sized_hedge = sizer.size_open(signal)
    assert sized_base.instrument_type == "STK"
    assert sized_hedge.instrument_type == "STK"
    assert sized_base.quantity == int(sized_base.quantity)
    assert sized_hedge.quantity == int(sized_hedge.quantity)
