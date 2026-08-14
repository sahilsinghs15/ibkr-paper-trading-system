"""Unit tests for CHECK 8 — MONEY PER STOCK."""

from decimal import Decimal

from app.rms.checks.money_per_stock import MoneyPerStockCheck
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
)


def test_money_per_stock_below_limit_passes() -> None:
    """Combined exposure below symbol limit passes Check 8."""
    check = MoneyPerStockCheck()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(
                strategy_id="MODEL_BLUE", max_open_positions=10, money_limit_per_symbol=Decimal(5000000)
            )
        },
        symbol_exposures={"RELIANCE": Decimal(3000000)},
    )
    # Order for 15 Lakh (Total = 45 Lakh < 50 Lakh limit)
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=600, price=Decimal(2500), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS


def test_money_per_stock_exactly_at_limit_passes() -> None:
    """Combined exposure exactly equal to symbol limit passes Check 8 (inclusive boundary)."""
    check = MoneyPerStockCheck()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(
                strategy_id="MODEL_BLUE", max_open_positions=10, money_limit_per_symbol=Decimal(5000000)
            )
        },
        symbol_exposures={"RELIANCE": Decimal(3500000)},
    )
    # Order for 15 Lakh (Total = 50 Lakh == 50 Lakh limit)
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=600, price=Decimal(2500), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS


def test_money_per_stock_above_limit_rejects() -> None:
    """Combined exposure exceeding symbol limit is rejected by Check 8."""
    check = MoneyPerStockCheck()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(
                strategy_id="MODEL_BLUE", max_open_positions=10, money_limit_per_symbol=Decimal(5000000)
            )
        },
        symbol_exposures={"RELIANCE": Decimal(3000000)},
    )
    # Order for 25 Lakh (Total = 55 Lakh > 50 Lakh limit)
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=1000, price=Decimal(2500), contract_month="2026-09")],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.check_number == 8
    assert "MONEY_LIMIT_EXCEEDED" in (result.reason or "")


def test_money_per_stock_accumulates_multiple_legs_same_symbol() -> None:
    """Multiple legs for the same symbol in a single intent are accumulated correctly."""
    check = MoneyPerStockCheck()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(
                strategy_id="MODEL_BLUE", max_open_positions=10, money_limit_per_symbol=Decimal(5000000)
            )
        },
        symbol_exposures={"RELIANCE": Decimal(3000000)},
    )
    # Leg 1: 10 Lakh, Leg 2: 15 Lakh -> New order notional = 25 Lakh (Total = 55 Lakh > 50 Lakh)
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=400, price=Decimal(2500), contract_month="2026-09"),
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=600, price=Decimal(2500), contract_month="2026-09"),
        ],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT


def test_money_per_stock_independent_symbols() -> None:
    """Exposures for different symbols are calculated independently."""
    check = MoneyPerStockCheck()
    context = RMSContext(
        strategy_configs={
            "MODEL_BLUE": StrategyConfig(
                strategy_id="MODEL_BLUE", max_open_positions=10, money_limit_per_symbol=Decimal(5000000)
            )
        },
        symbol_exposures={
            "RELIANCE": Decimal(4000000),
            "TCS": Decimal(1000000),
        },
    )
    # RELIANCE + 5 Lakh = 45 Lakh (PASS), TCS + 5 Lakh = 15 Lakh (PASS)
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=200, price=Decimal(2500), contract_month="2026-09"),
            OrderLeg(symbol="TCS", side=OrderSide.BUY, quantity=100, price=Decimal(5000), contract_month="2026-09"),
        ],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.PASS
