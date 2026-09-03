"""Unit tests for CHECK 2 — DUPLICATE."""

from decimal import Decimal

from app.rms.checks.duplicate import DuplicateCheck
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
)


def test_duplicate_check_new_open_signal_passes() -> None:
    """New OPEN signal with an unseen (strategy_id, signal_id) tuple passes Check 2."""
    check = DuplicateCheck()
    context = RMSContext(processed_signals={("model_blue", "SIG_001")})
    intent = OrderIntent(
        signal_id="SIG_002",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS


def test_duplicate_check_existing_open_signal_rejected() -> None:
    """Duplicate OPEN signal with already processed (strategy_id, signal_id) is rejected."""
    check = DuplicateCheck()
    context = RMSContext(processed_signals={("model_blue", "SIG_001")})
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.check_number == 2
    assert "DUPLICATE_SIGNAL" in (result.reason or "")


def test_duplicate_check_close_signal_does_not_block() -> None:
    """CLOSE signal with an already processed (strategy_id, signal_id) must NOT be blocked."""
    check = DuplicateCheck()
    context = RMSContext(processed_signals={("model_blue", "SIG_001")})
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.CLOSE,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.SELL, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS


def test_duplicate_key_deterministic_and_strategy_isolated() -> None:
    """Same signal_id for a different strategy_id is NOT a duplicate."""
    check = DuplicateCheck()
    context = RMSContext(processed_signals={("model_blue", "SIG_001")})
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_WHITE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(150), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS
