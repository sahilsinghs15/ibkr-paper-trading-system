"""Unit tests for RMSEngine pipeline orchestration."""

from datetime import UTC, datetime
from decimal import Decimal

from app.rms.engine import RMSEngine
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
)


def test_engine_runs_all_checks_in_exact_order_and_passes() -> None:
    """Full check pipeline runs all 5 checks in exact sequence and returns PASS."""
    engine = RMSEngine()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(
                strategy_id="MODEL_BLUE", max_open_positions=10, money_limit_per_symbol=Decimal(5000000)
            )
        },
        open_positions={"MODEL_BLUE": 2},
        symbol_exposures={"AAPL": Decimal(1000000)},
        current_time=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=100, price=Decimal(175), contract_month="2026-09"),
        ],
    )
    result = engine.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS
    assert len(result.check_results) == 5
    assert [c.check_number for c in result.check_results] == [2, 3, 4, 7, 8]


def test_engine_short_circuits_on_rejection() -> None:
    """Pipeline stops immediately when Check 3 fails; subsequent checks (4, 7, 8) do NOT execute."""
    engine = RMSEngine()
    context = RMSContext(strategy_configs={})
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="UNKNOWN_STRATEGY",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=100, price=Decimal(175), contract_month="2026-09"),
        ],
    )
    result = engine.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.check_number == 3
    # Check results should only contain Check 2 (PASS) and Check 3 (REJECT)
    assert len(result.check_results) == 2
    assert [c.check_number for c in result.check_results] == [2, 3]


def test_engine_applies_adjust_and_continues() -> None:
    """Check 4 ADJUST modifies intent in-place and subsequent checks evaluate the adjusted intent."""
    engine = RMSEngine()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(
                strategy_id="MODEL_BLUE", max_open_positions=10, money_limit_per_symbol=Decimal(5000000)
            )
        },
        current_time=datetime(2026, 9, 28, 10, 0, tzinfo=UTC),  # Rollover window active
        rollover_window_days=7,
    )
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=100, price=Decimal(2500), contract_month="2026-09", instrument_type="FUT"),
        ],
    )
    result = engine.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS
    assert len(result.check_results) == 5
    assert result.original_intent.legs[0].contract_month == "2026-09"
    assert result.intent.legs[0].contract_month == "2026-10"


def test_engine_audit_log_fields() -> None:
    """RMSResult contains complete structured audit information."""
    engine = RMSEngine()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(
                strategy_id="MODEL_BLUE", max_open_positions=10, money_limit_per_symbol=Decimal(5000000)
            )
        }
    )
    intent = OrderIntent(
        signal_id="SIG_123",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09"),
        ],
    )
    result = engine.evaluate(intent, context)
    assert result.timestamp is not None
    assert result.original_intent == intent
    assert result.intent == intent
    assert len(result.check_results) == 5
