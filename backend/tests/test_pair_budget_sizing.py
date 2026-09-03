"""Weight-proportional pair-budget sizing tests for Model Blue."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.signal import Signal, SignalLeg, SignalType
from app.rms.models import OrderSide
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.model_blue.parser import ModelBlueValidationError
from app.services.model_blue.sizer import ModelBlueSizer, SizedModelBlueLeg

_TS = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _sizer(**kwargs) -> ModelBlueSizer:
    return ModelBlueSizer(
        TemporarySettingsCommittedCapitalProvider(Decimal(10000)),
        min_order_notional=kwargs.pop("min_order_notional", Decimal(1)),
        **kwargs,
    )


def _open_signal(
    *,
    weights: tuple[float, float] = (0.5, -0.5),
    prices: tuple[Decimal, Decimal] = (Decimal(20), Decimal(12)),
    direction: int = 1,
) -> Signal:
    return Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="pair-budget",
        strategy_id="model_blue",
        action="OPEN",
        trade_id="T-PAIR",
        direction=direction,
        legs=(
            SignalLeg("AAA", "STK", weights[0], prices[0], payload_side=None),
            SignalLeg("BBB", "STK", weights[1], prices[1], payload_side=None),
        ),
    )


def test_sizer_is_weight_proportional() -> None:
    # direction +1; A w +0.50 @ $20; B w -0.50 @ $12; pair budget $50
    a, b = _sizer().size_open(_open_signal(), pair_budget=Decimal(50))
    assert a.side is OrderSide.BUY
    assert a.quantity == Decimal(1)  # 50*0.5/20 = 1.25 -> 1
    assert b.side is OrderSide.SELL
    assert b.quantity == Decimal(2)  # 50*0.5/12 = 2.08 -> 2
    assert a.notional + b.notional <= Decimal(50)


def test_unbalanced_pair_deploys_exactly_the_budget() -> None:
    """Regression: old anchor-gets-full yielded 1000+667 from a 1000 budget."""
    signal = _open_signal(
        weights=(0.6, -0.4),
        prices=(Decimal(10), Decimal(10)),
    )
    a, b = _sizer().size_open(signal, pair_budget=Decimal(1000))
    assert a.notional == Decimal(600)
    assert b.notional == Decimal(400)
    assert a.notional + b.notional == Decimal(1000)


@pytest.mark.parametrize("weights", [(0.4, -0.5), (0.6, -0.5)])
def test_weight_sum_rejected_both_directions(weights: tuple[float, float]) -> None:
    with pytest.raises(ModelBlueValidationError, match="MODEL_BLUE_WEIGHT_SUM_INVALID"):
        _sizer().size_open(_open_signal(weights=weights), pair_budget=Decimal(1000))


def test_weight_sum_inside_tolerance_passes() -> None:
    signal = _open_signal(
        weights=(0.5000001, -0.4999999),
        prices=(Decimal(10), Decimal(10)),
    )
    a, b = _sizer().size_open(signal, pair_budget=Decimal(1000))
    assert a.quantity > 0
    assert b.quantity > 0


def test_unnormalised_1_minus_0_7_rejected() -> None:
    """70%-overrun case from Part 0.3: weights 1.0 / -0.7 must not size."""
    with pytest.raises(ModelBlueValidationError, match="MODEL_BLUE_WEIGHT_SUM_INVALID"):
        _sizer().size_open(
            _open_signal(weights=(1.0, -0.7)),
            pair_budget=Decimal(50),
        )


def test_zero_budget_rejected() -> None:
    with pytest.raises(ModelBlueValidationError, match="MODEL_BLUE_PAIR_BUDGET_NOT_CONFIGURED"):
        _sizer().size_open(_open_signal(), pair_budget=Decimal(0))


def test_unset_provider_budget_rejected() -> None:
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(None))
    with pytest.raises(ModelBlueValidationError, match="MODEL_BLUE_PAIR_BUDGET_NOT_CONFIGURED"):
        sizer.size_open(_open_signal())


def test_leg_below_min_notional() -> None:
    sizer = _sizer(min_order_notional=Decimal(100))
    with pytest.raises(ModelBlueValidationError, match="MODEL_BLUE_MIN_NOTIONAL"):
        sizer.size_open(_open_signal(), pair_budget=Decimal(50))


def test_direction_minus_one_flips_both_sides() -> None:
    a, b = _sizer().size_open(
        _open_signal(direction=-1, prices=(Decimal(10), Decimal(10))),
        pair_budget=Decimal(1000),
    )
    assert a.side is OrderSide.SELL
    assert b.side is OrderSide.BUY


def test_budget_scaling_is_linear() -> None:
    signal = _open_signal(prices=(Decimal(10), Decimal(10)))
    small = _sizer().size_open(signal, pair_budget=Decimal(1000))
    large = _sizer().size_open(signal, pair_budget=Decimal(2000))
    assert large[0].quantity == small[0].quantity * 2
    assert large[1].quantity == small[1].quantity * 2


def test_ratio_drift_rejected_at_small_budget() -> None:
    # budget 50, weights 0.5/0.5, A @ $13, B @ $12
    #   A: 25/13 = 1.92 -> 1 share -> $13
    #   B: 25/12 = 2.08 -> 2 shares -> $24
    #   total 37; shares 0.351 / 0.649; worst_dev 0.149 > 0.05
    signal = _open_signal(prices=(Decimal(13), Decimal(12)))
    with pytest.raises(ModelBlueValidationError, match="MODEL_BLUE_RATIO_DRIFT"):
        _sizer().size_open(signal, pair_budget=Decimal(50))


def test_same_pair_passes_at_larger_budget() -> None:
    signal = _open_signal(prices=(Decimal(13), Decimal(12)))
    a, b = _sizer().size_open(signal, pair_budget=Decimal(5000))
    assert a.notional == Decimal(2496)
    assert b.notional == Decimal(2496)


def test_four_point_five_pp_deviation_passes_default_tolerance() -> None:
    # budget 50, A @ $20, B @ $12 -> $20 / $24, shares 0.4545/0.5455, dev 0.0455
    a, b = _sizer().size_open(_open_signal(), pair_budget=Decimal(50))
    assert a.notional + b.notional <= Decimal(50)


def test_tolerance_honoured_from_config() -> None:
    sizer = _sizer(ratio_tolerance=Decimal("0.03"))
    with pytest.raises(ModelBlueValidationError, match="MODEL_BLUE_RATIO_DRIFT"):
        sizer.size_open(_open_signal(), pair_budget=Decimal(50))


def test_unbalanced_intended_weights_compared_against_themselves() -> None:
    """Catches an implementation that hardcodes an equal 0.5/0.5 split."""
    signal = _open_signal(
        weights=(0.8, -0.2),
        prices=(Decimal(10), Decimal(10)),
    )
    a, b = _sizer().size_open(signal, pair_budget=Decimal(1000))
    assert a.notional == Decimal(800)
    assert b.notional == Decimal(200)


def test_min_deployment_rejects_when_enabled() -> None:
    # $20 + $24 = $44 of $50 = 88%
    sizer = _sizer(min_deployment=Decimal("0.9"))
    with pytest.raises(ModelBlueValidationError, match="MODEL_BLUE_UNDER_DEPLOYED"):
        sizer.size_open(_open_signal(), pair_budget=Decimal(50))


def test_min_deployment_off_by_default() -> None:
    a, b = _sizer().size_open(_open_signal(), pair_budget=Decimal(50))
    assert a.notional + b.notional < Decimal(50)


def test_three_leg_ratio_check_is_per_leg() -> None:
    sizer = _sizer()
    sized = (
        SizedModelBlueLeg(
            symbol="A",
            instrument_type="STK",
            side=OrderSide.BUY,
            quantity=Decimal(5),
            price=Decimal(10),
            notional=Decimal(50),
            weight=0.5,
        ),
        SizedModelBlueLeg(
            symbol="B",
            instrument_type="STK",
            side=OrderSide.SELL,
            quantity=Decimal(3),
            price=Decimal(10),
            notional=Decimal(30),
            weight=-0.3,
        ),
        SizedModelBlueLeg(
            symbol="C",
            instrument_type="STK",
            side=OrderSide.BUY,
            quantity=Decimal(2),
            price=Decimal(10),
            notional=Decimal(20),
            weight=0.2,
        ),
    )
    sizer._validate_realised_ratio(sized, budget=Decimal(100), trade_id="T-3")
