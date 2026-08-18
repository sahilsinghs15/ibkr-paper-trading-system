"""Unit tests for CHECK 4 — CONTRACT MONTH."""

from datetime import UTC, datetime
from decimal import Decimal

from app.rms.checks.contract_month import ContractMonthCheck, get_next_contract_month
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
)


def test_get_next_contract_month_utility() -> None:
    """Utility correctly increments month string across month/year boundaries."""
    assert get_next_contract_month("2026-09") == "2026-10"
    assert get_next_contract_month("2026-12") == "2027-01"


def test_contract_month_normal_period_passes_unchanged() -> None:
    """Normal trading period outside rollover window leaves contract month unchanged."""
    check = ContractMonthCheck()
    context = RMSContext(
        current_time=datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
        rollover_window_days=7,
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=100, price=Decimal(2500), contract_month="2026-09", instrument_type="FUT"),
            OrderLeg(symbol="TCS", side=OrderSide.SELL, quantity=50, price=Decimal(3500), contract_month="2026-09", instrument_type="FUT"),
        ],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS
    assert result.adjusted_intent is None


def test_contract_month_rollover_period_adjusts_both_legs() -> None:
    """Rollover period triggers ADJUST and updates both legs to the next contract month."""
    check = ContractMonthCheck()
    context = RMSContext(
        current_time=datetime(2026, 9, 28, 10, 0, tzinfo=UTC),
        rollover_window_days=7,
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=100, price=Decimal(2500), contract_month="2026-09", instrument_type="FUT"),
            OrderLeg(symbol="TCS", side=OrderSide.SELL, quantity=50, price=Decimal(3500), contract_month="2026-09", instrument_type="FUT"),
        ],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.ADJUST
    assert result.adjusted_intent is not None
    assert result.adjusted_intent.legs[0].contract_month == "2026-10"
    assert result.adjusted_intent.legs[1].contract_month == "2026-10"


def test_contract_month_mismatched_legs_normalized() -> None:
    """Mismatched leg contract months are normalized to the target contract month."""
    check = ContractMonthCheck()
    context = RMSContext(
        current_time=datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
        target_rollover_month="2026-10",
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=100, price=Decimal(2500), contract_month="2026-09", instrument_type="FUT"),
            OrderLeg(symbol="TCS", side=OrderSide.SELL, quantity=50, price=Decimal(3500), contract_month="2026-10", instrument_type="FUT"),
        ],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.ADJUST
    assert result.adjusted_intent is not None
    assert result.adjusted_intent.legs[0].contract_month == "2026-10"
    assert result.adjusted_intent.legs[1].contract_month == "2026-10"


def test_contract_month_configurable_rollover_checker() -> None:
    """Custom rollover_checker callback rule can be injected into RMSContext."""
    check = ContractMonthCheck()
    # Custom rule: always trigger rollover if contract_month starts with '2026-09'
    context = RMSContext(
        rollover_checker=lambda month, dt, window: month == "2026-09"
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=100, price=Decimal(2500), contract_month="2026-09", instrument_type="FUT"),
        ],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.ADJUST
    assert result.adjusted_intent is not None
    assert result.adjusted_intent.legs[0].contract_month == "2026-10"
