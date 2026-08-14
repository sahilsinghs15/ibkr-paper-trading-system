"""Unit tests for CHECK 3 — STRATEGY."""

from decimal import Decimal

from app.rms.checks.strategy import StrategyCheck
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
)


def test_strategy_check_valid_strategy_passes() -> None:
    """Configured strategy_id passes Check 3."""
    check = StrategyCheck()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(strategy_id="MODEL_BLUE", max_open_positions=10)
        }
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS


def test_strategy_check_unknown_strategy_rejected() -> None:
    """Unconfigured strategy_id is rejected by Check 3."""
    check = StrategyCheck()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(strategy_id="MODEL_BLUE", max_open_positions=10)
        }
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="UNKNOWN_STRAT",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.check_number == 3
    assert "UNKNOWN_STRATEGY" in (result.reason or "")


def test_strategy_check_missing_strategy_rejected() -> None:
    """Empty or whitespace strategy_id is rejected by Check 3."""
    check = StrategyCheck()
    context = RMSContext()
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.check_number == 3
    assert "MISSING_STRATEGY_ID" in (result.reason or "")
