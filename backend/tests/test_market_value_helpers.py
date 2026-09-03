"""Market-value helper tests: gross, summed, never netted."""

from decimal import Decimal
from types import SimpleNamespace

from app.rms.market_value import (
    intent_market_value,
    leg_market_value,
    position_row_market_value,
)
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide


def test_leg_market_value_equals_notional() -> None:
    leg = OrderLeg(
        symbol="XLE",
        side=OrderSide.BUY,
        quantity=10,
        price=Decimal(20),
        contract_month="2026-09",
        notional=Decimal(200),
    )
    assert leg_market_value(leg) == Decimal(200)


def test_short_leg_yields_positive_value() -> None:
    """A sign error here understates exposure on every hedge leg."""
    leg = OrderLeg(
        symbol="XOP",
        side=OrderSide.SELL,
        quantity=-10,
        price=Decimal(20),
        contract_month="2026-09",
    )
    assert leg.effective_notional < 0
    assert leg_market_value(leg) == Decimal(200)


def test_intent_market_value_sums_never_nets() -> None:
    intent = OrderIntent(
        signal_id="T1",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="XLE",
                side=OrderSide.BUY,
                quantity=1,
                price=Decimal(25),
                contract_month="2026-09",
                notional=Decimal(25),
            ),
            OrderLeg(
                symbol="XOP",
                side=OrderSide.SELL,
                quantity=1,
                price=Decimal(25),
                contract_month="2026-09",
                notional=Decimal(25),
            ),
        ],
    )
    assert intent_market_value(intent) == Decimal(50)


def test_position_row_single_leg_counts_leg_a_only() -> None:
    row = SimpleNamespace(
        leg_a_signed_qty=Decimal(10),
        leg_a_entry_mark=Decimal(20),
        leg_b_symbol=None,
        leg_b_signed_qty=None,
        leg_b_entry_mark=None,
    )
    assert position_row_market_value(row) == Decimal(200)


def test_position_row_both_legs_summed() -> None:
    row = SimpleNamespace(
        leg_a_signed_qty=Decimal(10),
        leg_a_entry_mark=Decimal(20),
        leg_b_symbol="XOP",
        leg_b_signed_qty=Decimal(-5),
        leg_b_entry_mark=Decimal(12),
    )
    assert position_row_market_value(row) == Decimal(260)
