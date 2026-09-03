"""Unit tests for CHECK 7 — OPEN POSITION LIMIT."""

from decimal import Decimal

from app.rms.checks.position_limit import OpenPositionLimitCheck
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
    open_position_key,
)


def test_position_limit_below_limit_passes() -> None:
    """Strategy with open positions strictly below max limit passes Check 7."""
    check = OpenPositionLimitCheck()
    context = RMSContext(
        strategy_configs={"MODEL_BLUE": StrategyConfig(strategy_id="MODEL_BLUE", max_open_positions=10)},
        open_positions={"MODEL_BLUE": 9},
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS


def test_position_limit_exactly_at_limit_rejects() -> None:
    """Strategy with open positions equal to max limit is rejected by Check 7."""
    check = OpenPositionLimitCheck()
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    context = RMSContext(
        strategy_configs={"MODEL_BLUE": StrategyConfig(strategy_id="MODEL_BLUE", max_open_positions=10)},
        open_positions={open_position_key(intent): 10},
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.check_number == 7
    assert "OPEN_POSITION_LIMIT_REACHED" in (result.reason or "")


def test_position_limit_above_limit_rejects() -> None:
    """Strategy with open positions above max limit is rejected by Check 7."""
    check = OpenPositionLimitCheck()
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    context = RMSContext(
        strategy_configs={"MODEL_BLUE": StrategyConfig(strategy_id="MODEL_BLUE", max_open_positions=10)},
        open_positions={open_position_key(intent): 11},
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.check_number == 7


def test_position_limit_close_signal_bypasses() -> None:
    """CLOSE action is not blocked even if open positions meet or exceed max limit."""
    check = OpenPositionLimitCheck()
    context = RMSContext(
        strategy_configs={"MODEL_BLUE": StrategyConfig(strategy_id="MODEL_BLUE", max_open_positions=10)},
        open_positions={"MODEL_BLUE": 10},
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.CLOSE,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.SELL, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS
