"""Unit tests for production Model Blue parser and sizer (no reference helper)."""

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import pytest

from app.models.signal import Signal, SignalLeg, SignalType
from app.rms.models import OrderSide
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.model_blue.parser import (
    ModelBlueValidationError,
    parse_model_blue_payload,
)
from app.services.model_blue.sizer import ModelBlueSizer

_COMMITTED = Decimal(25000)
_TS = datetime(2026, 8, 17, 19, 55, tzinfo=UTC)


def _qty(notional: Decimal, price: Decimal) -> Decimal:
    return (notional / price).quantize(Decimal("1"), rounding=ROUND_DOWN)


def test_parser_preserves_both_open_legs() -> None:
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": "MBG-AMEX:XLE-AMEX:XOP-20260817T1550",
        "direction": 1,
        "buckets": [
            {
                "underlying": "XLE",
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "BUY",
                        "weight": 0.5943,
                        "price": 62.59,
                    }
                ],
            },
            {
                "underlying": "XOP",
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "SELL",
                        "weight": -0.4057,
                        "price": 183.34,
                    }
                ],
            },
        ],
    }
    signal = parse_model_blue_payload(payload, timestamp=_TS, reason="test")
    assert signal.strategy_id == "model_blue"
    assert signal.action == "OPEN"
    assert signal.trade_id == "MBG-AMEX:XLE-AMEX:XOP-20260817T1550"
    assert signal.direction == 1
    assert len(signal.legs) == 2
    assert signal.legs[0].symbol == "XLE"
    assert signal.legs[0].weight == 0.5943
    assert signal.legs[0].price == Decimal("62.59")
    assert signal.legs[1].symbol == "XOP"
    assert signal.legs[1].weight == -0.4057
    assert signal.quantity is None


def test_parser_close_has_no_legs_or_quantity() -> None:
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "CLOSE",
        "trade_id": "MBG-AMEX:EWA-AMEX:EWC-20260817T1505",
        "direction": 1,
    }
    signal = parse_model_blue_payload(payload, timestamp=_TS, reason="test")
    assert signal.action == "CLOSE"
    assert signal.legs == ()
    assert signal.quantity is None
    assert signal.symbol is None


def test_sizer_direction_plus_one_uses_weight_times_direction() -> None:
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="test",
        strategy_id="model_blue",
        action="OPEN",
        trade_id="T1",
        direction=1,
        legs=(
            SignalLeg("XLE", "STK", 0.5943, Decimal("62.59"), payload_side="BUY"),
            SignalLeg("XOP", "STK", -0.4057, Decimal("183.34"), payload_side="SELL"),
        ),
    )
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(_COMMITTED))
    xle, xop = sizer.size_open(signal)
    base_w = Decimal("0.5943")
    xop_target = _COMMITTED * Decimal("0.4057") / base_w
    assert xle.side == OrderSide.BUY
    assert xop.side == OrderSide.SELL
    assert xle.quantity == Decimal("399")
    assert xop.quantity == Decimal("93")
    assert xle.quantity == _qty(_COMMITTED, Decimal("62.59"))
    assert xop.quantity == _qty(xop_target, Decimal("183.34"))
    assert xle.quantity != Decimal(1)
    assert xop.quantity != Decimal(1)


def test_sizer_direction_minus_one_ignores_payload_side() -> None:
    """HYG payload_side=SELL and LQD payload_side=BUY must not override weight × direction."""
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="test",
        strategy_id="model_blue",
        action="OPEN",
        trade_id="T2",
        direction=-1,
        legs=(
            SignalLeg("HYG", "STK", -0.6978, Decimal("79.65"), payload_side="SELL"),
            SignalLeg("LQD", "STK", 0.3022, Decimal("105.79"), payload_side="BUY"),
        ),
    )
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(_COMMITTED))
    hyg, lqd = sizer.size_open(signal)
    assert hyg.side == OrderSide.BUY  # (-0.6978) * (-1) > 0
    assert lqd.side == OrderSide.SELL  # (0.3022) * (-1) < 0


def test_sizer_rejects_unset_committed_capital() -> None:
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="test",
        strategy_id="model_blue",
        action="OPEN",
        trade_id="T3",
        direction=1,
        legs=(
            SignalLeg("XLE", "STK", 0.5, Decimal(10), payload_side=None),
            SignalLeg("XOP", "STK", -0.5, Decimal(10), payload_side=None),
        ),
    )
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(None))
    with pytest.raises(ModelBlueValidationError, match="COMMITTED_NOT_CONFIGURED"):
        sizer.size_open(signal)


def test_sizer_does_not_size_close() -> None:
    signal = Signal(
        signal_type=SignalType.SELL,
        timestamp=_TS,
        reason="test",
        strategy_id="model_blue",
        action="CLOSE",
        trade_id="T4",
        direction=1,
        legs=(),
    )
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(_COMMITTED))
    with pytest.raises(ModelBlueValidationError, match="does not size CLOSE"):
        sizer.size_open(signal)


def test_sizer_rejects_stk_below_one_share() -> None:
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="test",
        strategy_id="model_blue",
        action="OPEN",
        trade_id="T5",
        direction=1,
        legs=(
            SignalLeg("XLE", "STK", 0.5, Decimal("25000"), payload_side=None),
            SignalLeg("XOP", "STK", -0.5, Decimal("10"), payload_side=None),
        ),
    )
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(Decimal("100")))
    with pytest.raises(ModelBlueValidationError, match="MIN_SHARE"):
        sizer.size_open(signal)


def test_parser_missing_instrument_type_does_not_become_stk() -> None:
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": "T-NO-TYPE",
        "direction": 1,
        "buckets": [
            {
                "underlying": "SIL",
                "legs": [{"side": "BUY", "weight": 0.5, "price": 90.64}],
            },
            {
                "underlying": "GDX",
                "legs": [
                    {"instrument_type": "STK", "side": "SELL", "weight": -0.5, "price": 91.86}
                ],
            },
        ],
    }
    with pytest.raises(ModelBlueValidationError, match="MISSING_INSTRUMENT_TYPE"):
        parse_model_blue_payload(payload, timestamp=_TS, reason="t")


def test_parser_preserves_instrument_type_side_weight_price() -> None:
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": "T-SIL-GDX",
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
    assert signal.strategy_id == "model_blue"
    assert signal.trade_id == "T-SIL-GDX"
    assert signal.market == "SMART"
    assert signal.legs[0].instrument_type == "STK"
    assert signal.legs[0].payload_side == "BUY"
    assert signal.legs[0].weight == 0.5019
    assert signal.legs[0].price == Decimal("90.64")
    assert signal.legs[1].instrument_type == "STK"
    assert signal.legs[1].payload_side == "SELL"


def test_sizer_floors_stk_example_quantities() -> None:
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="t",
        strategy_id="model_blue",
        action="OPEN",
        trade_id="T-FLOOR",
        direction=1,
        legs=(
            SignalLeg("XLE", "STK", 0.5943, Decimal("62.59"), payload_side="BUY"),
            SignalLeg("XOP", "STK", -0.4057, Decimal("183.34"), payload_side="SELL"),
        ),
    )
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(Decimal(25000)))
    xle, xop = sizer.size_open(signal)
    assert xle.quantity == Decimal(399)
    assert xop.quantity == Decimal(93)


def test_sizer_does_not_floor_cfd() -> None:
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="t",
        strategy_id="model_blue",
        action="OPEN",
        trade_id="T-CFD-QTY",
        direction=1,
        legs=(
            SignalLeg("SIL", "CFD", 0.5, Decimal("90.64"), payload_side="BUY"),
            SignalLeg("GDX", "CFD", -0.5, Decimal("91.86"), payload_side="SELL"),
        ),
    )
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(Decimal(25000)))
    sil, gdx = sizer.size_open(signal)
    assert sil.quantity != sil.quantity.to_integral_value()
    assert gdx.quantity != gdx.quantity.to_integral_value()
    assert sil.quantity == (Decimal(25000) / Decimal("90.64")).quantize(Decimal("0.0001"))
