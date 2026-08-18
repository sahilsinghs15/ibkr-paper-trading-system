"""Instrument/contract resolution. No live Gateway."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.tws_client import TWSClient
from app.instruments.models import InstrumentRecord, InstrumentResolutionError
from app.instruments.resolver import (
    InMemoryInstrumentCatalog,
    apply_size_increment,
    attach_resolved,
    ibkr_contract_from_resolved,
    resolve_leg,
)
from app.oms.coordinator import BasketCoordinator
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.oms_service import OMSService
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide, RMSOutcome, RMSResult
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.model_blue.parser import parse_model_blue_payload
from app.services.model_blue.sizer import ModelBlueSizer
from tests.ibkr_test_utils import fill_on_place_order

_TS = datetime(2026, 8, 18, 17, 0, tzinfo=UTC)


def _stk_leg(symbol: str, qty: float = 10.0, index: int = 0) -> OrderLeg:
    return OrderLeg(
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=qty,
        price=Decimal("10"),
        contract_month="2026-09",
        instrument_type="STK",
        leg_index=index,
    )


def _cfd_record(symbol: str, conid: int = 111) -> InstrumentRecord:
    return InstrumentRecord(
        symbol=symbol,
        sec_type="CFD",
        trade_conid=conid,
        market_data_conid=conid + 1,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal(1),
        underlying_exchange="ARCA",
    )


def test_stk_resolves_to_stk_smart_usd_without_master() -> None:
    resolved = resolve_leg(symbol="SIL", instrument_type="STK")
    assert resolved.sec_type == "STK"
    assert resolved.requested_instrument_type == "STK"
    assert resolved.exchange == "SMART"
    assert resolved.currency == "USD"
    assert resolved.con_id is None
    contract = ibkr_contract_from_resolved(resolved)
    assert contract.secType == "STK"
    assert contract.symbol == "SIL"


def test_etf_explicitly_maps_to_ibkr_stk() -> None:
    resolved = resolve_leg(symbol="SIL", instrument_type="ETF")
    assert resolved.sec_type == "STK"
    assert resolved.requested_instrument_type == "ETF"


def test_signal_market_overrides_default_exchange() -> None:
    resolved = resolve_leg(symbol="SIL", instrument_type="STK", market="NYSE")
    assert resolved.exchange == "NYSE"


def test_cfd_without_master_does_not_become_stk() -> None:
    with pytest.raises(InstrumentResolutionError, match="INSTRUMENT_METADATA_MISSING"):
        resolve_leg(symbol="SIL", instrument_type="CFD")
    try:
        resolve_leg(symbol="SIL", instrument_type="CFD")
    except InstrumentResolutionError as exc:
        assert "STK" in str(exc)
        assert "CFD" in str(exc)


def test_cfd_with_master_uses_conid() -> None:
    catalog = InMemoryInstrumentCatalog([_cfd_record("SIL", 4242)])
    resolved = resolve_leg(symbol="SIL", instrument_type="CFD", catalog=catalog)
    assert resolved.sec_type == "CFD"
    assert resolved.con_id == 4242
    contract = ibkr_contract_from_resolved(resolved)
    assert contract.secType == "CFD"
    assert contract.conId == 4242


def test_cfd_stk_row_is_not_a_fallback() -> None:
    catalog = InMemoryInstrumentCatalog(
        [
            InstrumentRecord(
                symbol="SIL",
                sec_type="STK",
                trade_conid=1,
                market_data_conid=2,
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
            )
        ]
    )
    with pytest.raises(InstrumentResolutionError, match="INSTRUMENT_METADATA_MISSING"):
        resolve_leg(symbol="SIL", instrument_type="CFD", catalog=catalog)


def test_missing_and_unsupported_types() -> None:
    with pytest.raises(InstrumentResolutionError, match="MISSING_INSTRUMENT_TYPE"):
        resolve_leg(symbol="SIL", instrument_type="")
    with pytest.raises(InstrumentResolutionError, match="UNSUPPORTED_INSTRUMENT_TYPE"):
        resolve_leg(symbol="SIL", instrument_type="OPT")


def test_ambiguous_instrument_rejected() -> None:
    catalog = InMemoryInstrumentCatalog(
        [_cfd_record("SIL", 1), _cfd_record("SIL", 2)]
    )
    with pytest.raises(InstrumentResolutionError, match="AMBIGUOUS_INSTRUMENT"):
        resolve_leg(symbol="SIL", instrument_type="CFD", catalog=catalog)


def test_invalid_conid_rejected() -> None:
    catalog = InMemoryInstrumentCatalog([_cfd_record("SIL", 0)])
    with pytest.raises(InstrumentResolutionError, match="INVALID_CONID"):
        resolve_leg(symbol="SIL", instrument_type="CFD", catalog=catalog)


def test_sil_gdx_stk_sizing_is_whole_share() -> None:
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": "MBG-SIL-GDX",
        "direction": 1,
        "buckets": [
            {
                "underlying": "SIL",
                "legs": [
                    {"instrument_type": "STK", "side": "BUY", "weight": 0.5019, "price": 90.64}
                ],
            },
            {
                "underlying": "GDX",
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "SELL",
                        "weight": -0.4981,
                        "price": 91.86,
                    }
                ],
            },
        ],
    }
    signal = parse_model_blue_payload(payload, timestamp=_TS, reason="t")
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(Decimal(25000)))
    sil, gdx = sizer.size_open(signal)
    assert sil.quantity == int(sil.quantity)
    assert gdx.quantity == int(gdx.quantity)
    assert sil.quantity >= 1
    assert gdx.quantity >= 1


@pytest.mark.asyncio
async def test_stk_reaches_ibkr_as_stk_and_adapter_does_not_floor() -> None:
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 10
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    fill_on_place_order(adapter, tws)
    oms = OMSService(adapter=adapter)
    intent = OrderIntent(
        signal_id="T-STK",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=399.4248,
                price=Decimal("90.64"),
                contract_month="2026-09",
                instrument_type="STK",
                leg_index=0,
            )
        ],
        timestamp=_TS,
    )
    rms = RMSResult(
        outcome=RMSOutcome.PASS, intent=intent, original_intent=intent, timestamp=_TS
    )
    res = await oms.submit_intent(intent, rms)
    assert res.order.resolved is not None
    assert res.order.resolved.sec_type == "STK"
    ib_order = tws.placeOrder.call_args.args[2]
    contract = tws.placeOrder.call_args.args[1]
    assert contract.secType == "STK"
    assert ib_order.totalQuantity == 399.4248


@pytest.mark.asyncio
async def test_cfd_reaches_ibkr_as_cfd() -> None:
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 10
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    fill_on_place_order(adapter, tws)
    oms = OMSService(adapter=adapter)
    catalog = InMemoryInstrumentCatalog([_cfd_record("SIL", 777)])
    intent = attach_resolved(
        OrderIntent(
            signal_id="T-CFD",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            legs=[
                OrderLeg(
                    symbol="SIL",
                    side=OrderSide.BUY,
                    quantity=10.25,
                    price=Decimal("90.64"),
                    contract_month="2026-09",
                    instrument_type="CFD",
                    leg_index=0,
                )
            ],
            timestamp=_TS,
        ),
        catalog=catalog,
    )
    rms = RMSResult(
        outcome=RMSOutcome.PASS, intent=intent, original_intent=intent, timestamp=_TS
    )
    res = await oms.submit_intent(intent, rms)
    contract = tws.placeOrder.call_args.args[1]
    assert contract.secType == "CFD"
    assert contract.conId == 777
    assert res.order.resolved.sec_type == "CFD"
    ib_order = tws.placeOrder.call_args.args[2]
    assert ib_order.totalQuantity == 10.25


@pytest.mark.asyncio
async def test_n_leg_independent_and_unresolved_leg_blocks_basket() -> None:
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 50
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    fill_on_place_order(adapter, tws)
    oms = OMSService(adapter=adapter)
    coord = BasketCoordinator(oms, fill_timeout=0.2)
    mixed = OrderIntent(
        signal_id="T-MIX",
        strategy_id="synthetic",
        action=OrderAction.OPEN,
        account_id=1,
        ibkr_account="DUTEST",
        legs=[
            _stk_leg("SIL", 10, 0),
            OrderLeg(
                symbol="GDX",
                side=OrderSide.SELL,
                quantity=10,
                price=Decimal("10"),
                contract_month="2026-09",
                instrument_type="CFD",
                leg_index=1,
            ),
        ],
        timestamp=_TS,
    )
    rms = RMSResult(
        outcome=RMSOutcome.PASS, intent=mixed, original_intent=mixed, timestamp=_TS
    )
    with pytest.raises(ValueError, match="INSTRUMENT_RESOLUTION_FAILED"):
        await coord.execute(mixed, rms, order_type="MARKET")
    tws.placeOrder.assert_not_called()


def test_pnl_does_not_request_stk_for_unresolved_cfd() -> None:
    from app.services.pnl import LivePnlService

    client = MagicMock()
    client.reqMktData = MagicMock()
    svc = LivePnlService(MagicMock(), client)
    intent = OrderIntent(
        signal_id="T-PNL-CFD",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        account_id=1,
        legs=[
            OrderLeg(
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal("90"),
                contract_month="2026-09",
                instrument_type="CFD",
            )
        ],
        timestamp=_TS,
    )
    svc.watch_open(intent)
    client.reqMktData.assert_not_called()


def test_pnl_subscribes_cfd_under_demo_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_EXECUTE_STK_AS_CFD", "true")
    from app.services.pnl import LivePnlService

    client = MagicMock()
    client.reqMktData = MagicMock()
    svc = LivePnlService(MagicMock(), client)
    intent = OrderIntent(
        signal_id="T-PNL-CFD-DEMO",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        account_id=1,
        legs=[
            OrderLeg(
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal("90.64"),
                contract_month="2026-09",
                instrument_type="CFD",
                leg_index=0,
            ),
            OrderLeg(
                symbol="GDX",
                side=OrderSide.SELL,
                quantity=10,
                price=Decimal("91.86"),
                contract_month="2026-09",
                instrument_type="CFD",
                leg_index=1,
            ),
        ],
        timestamp=_TS,
    )
    svc.watch_open(intent)
    assert client.reqMktData.call_count == 2
    contracts = [call.args[1] for call in client.reqMktData.call_args_list]
    assert {c.symbol for c in contracts} == {"SIL", "GDX"}
    assert all(c.secType == "CFD" for c in contracts)
    assert all(c.secType != "STK" for c in contracts)
    sil_req = next(rid for rid, mapped in svc._by_req.items() if mapped[2] == "SIL")
    gdx_req = next(rid for rid, mapped in svc._by_req.items() if mapped[2] == "GDX")
    svc.on_tick_price(sil_req, 4, 91.64)
    svc.on_tick_price(gdx_req, 4, 90.86)
    assert svc._marks[(1, "T-PNL-CFD-DEMO", "SIL")] == Decimal("91.64")
    assert svc._marks[(1, "T-PNL-CFD-DEMO", "GDX")] == Decimal("90.86")
    from app.services.pnl import unrealized_pair

    expected = unrealized_pair(
        leg_a_signed=Decimal("10"),
        leg_a_entry=Decimal("90.64"),
        leg_a_mark=Decimal("91.64"),
        leg_b_signed=Decimal("-10"),
        leg_b_entry=Decimal("91.86"),
        leg_b_mark=Decimal("90.86"),
    )
    assert expected == Decimal("20.00")


def test_discovered_sil_gdx_cfd_conids_are_not_stk() -> None:
    """Paper Gateway discovery: SIL 384919303 / GDX 134771127 are CFD, not STK 211651690/229726316."""
    catalog = InMemoryInstrumentCatalog(
        [
            InstrumentRecord(
                symbol="SIL",
                sec_type="CFD",
                trade_conid=384919303,
                market_data_conid=384919303,
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
                underlying_exchange="ARCA",
                size_increment=Decimal(1),
            ),
            InstrumentRecord(
                symbol="GDX",
                sec_type="CFD",
                trade_conid=134771127,
                market_data_conid=134771127,
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
                underlying_exchange="ARCA",
                size_increment=Decimal(1),
            ),
        ]
    )
    sil = resolve_leg(symbol="SIL", instrument_type="CFD", catalog=catalog)
    gdx = resolve_leg(symbol="GDX", instrument_type="CFD", catalog=catalog)
    assert sil.sec_type == "CFD"
    assert gdx.sec_type == "CFD"
    assert sil.con_id == 384919303
    assert gdx.con_id == 134771127
    assert sil.con_id != 211651690
    assert gdx.con_id != 229726316
    assert ibkr_contract_from_resolved(sil).secType == "CFD"
    assert ibkr_contract_from_resolved(gdx).secType == "CFD"


def test_size_increment_numeric_scale_still_whole_lot() -> None:
    qty = apply_size_increment(Decimal("275.8164"), Decimal("1.00000000"))
    assert qty == Decimal("275")


def test_cfd_size_increment_one_is_valid_for_ibkr_share_cfd() -> None:
    """IBKR 10318: these US ETF CFDs reject 0.0001; size_increment=1 is required."""
    catalog = InMemoryInstrumentCatalog(
        [
            InstrumentRecord(
                symbol="SIL",
                sec_type="CFD",
                trade_conid=384919303,
                market_data_conid=384919303,
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
                size_increment=Decimal("1.00000000"),
            )
        ]
    )
    intent = OrderIntent(
        signal_id="T-CFD-INC",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="SIL",
                side=OrderSide.BUY,
                quantity=275.8164,
                price=Decimal("90.64"),
                contract_month="2026-09",
                instrument_type="CFD",
                leg_index=0,
            )
        ],
        timestamp=_TS,
    )
    resolved_intent = attach_resolved(intent, catalog=catalog)
    assert resolved_intent.legs[0].quantity == 275.0
    assert resolved_intent.legs[0].resolved is not None
    assert resolved_intent.legs[0].resolved.sec_type == "CFD"
    contract = ibkr_contract_from_resolved(resolved_intent.legs[0].resolved)
    assert contract.secType == "CFD"
    assert contract.conId == 384919303

