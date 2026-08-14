#!/usr/bin/env python3
"""RMS Standalone Foundation Demo Harness.

Demonstrates all 5 implemented RMS checks in isolation across 6 key scenarios.
This script DOES NOT place orders or interact with external systems.
"""

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

# Add backend directory to sys.path if needed
backend_dir = Path(__file__).resolve().parents[2]
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

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


def print_separator(title: str) -> None:
    print("\n" + "=" * 70)
    print(f" SCENARIO: {title}")
    print("=" * 70)


def print_result(scenario_num: int, title: str, result) -> None:
    print(f"Scenario {scenario_num} Status: [{result.outcome.value}]")
    if result.check_number:
        print(f"Failed at Check {result.check_number}: {result.reason}")

    print("\nCheck Audit Trail:")
    for check_res in result.check_results:
        status = check_res.outcome.value
        note = f" -> {check_res.reason}" if check_res.reason else ""
        print(f"  - Check {check_res.check_number} ({check_res.check_name}): [{status}]{note}")

    if result.outcome == RMSOutcome.PASS and result.intent != result.original_intent:
        print("\nAdjusted Order Intent:")
        for leg in result.intent.legs:
            print(f"  - {leg.side.value} {leg.quantity} {leg.symbol} ContractMonth={leg.contract_month}")


def main() -> None:
    print("🚀 Running Risk Management System (RMS) v1 Standalone Demo...")
    engine = RMSEngine()

    # Standard strategy configs
    configs = {
        "MODEL_BLUE": StrategyConfig(
            strategy_id="MODEL_BLUE",
            max_open_positions=10,
            money_limit_per_symbol=Decimal(5000000),  # 50 Lakh
        ),
        "MODEL_WHITE": StrategyConfig(
            strategy_id="MODEL_WHITE",
            max_open_positions=5,
            money_limit_per_symbol=Decimal(2000000),  # 20 Lakh
        ),
    }

    # ----------------------------------------------------
    # Scenario 1: Valid Order → PASS
    # ----------------------------------------------------
    print_separator("1. Valid Order -> Expected [PASS]")
    context_1 = RMSContext(
        strategy_configs=configs,
        open_positions={"MODEL_BLUE": 2},
        symbol_exposures={"AAPL": Decimal(1000000)},
        current_time=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
    )
    intent_1 = OrderIntent(
        signal_id="SIG_1001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=100, price=Decimal("175.00"), contract_month="2026-09"),
            OrderLeg(symbol="MSFT", side=OrderSide.SELL, quantity=50, price=Decimal("340.00"), contract_month="2026-09"),
        ],
    )
    res_1 = engine.evaluate(intent_1, context_1)
    print_result(1, "Valid Order", res_1)
    assert res_1.outcome == RMSOutcome.PASS

    # ----------------------------------------------------
    # Scenario 2: Duplicate Signal → REJECT
    # ----------------------------------------------------
    print_separator("2. Duplicate Signal -> Expected [REJECT]")
    context_2 = RMSContext(
        processed_signals={("MODEL_BLUE", "SIG_1002")},
        strategy_configs=configs,
    )
    intent_2 = OrderIntent(
        signal_id="SIG_1002",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal("175.00"), contract_month="2026-09"),
        ],
    )
    res_2 = engine.evaluate(intent_2, context_2)
    print_result(2, "Duplicate Signal", res_2)
    assert res_2.outcome == RMSOutcome.REJECT
    assert res_2.check_number == 2

    # ----------------------------------------------------
    # Scenario 3: Unknown Strategy → REJECT
    # ----------------------------------------------------
    print_separator("3. Unknown Strategy -> Expected [REJECT]")
    context_3 = RMSContext(strategy_configs=configs)
    intent_3 = OrderIntent(
        signal_id="SIG_1003",
        strategy_id="MODEL_UNKNOWN",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal("175.00"), contract_month="2026-09"),
        ],
    )
    res_3 = engine.evaluate(intent_3, context_3)
    print_result(3, "Unknown Strategy", res_3)
    assert res_3.outcome == RMSOutcome.REJECT
    assert res_3.check_number == 3

    # ----------------------------------------------------
    # Scenario 4: Contract Rollover → ADJUST → PASS
    # ----------------------------------------------------
    print_separator("4. Contract Rollover Period -> Expected [ADJUST -> PASS]")
    context_4 = RMSContext(
        strategy_configs=configs,
        current_time=datetime(2026, 9, 28, 10, 0, tzinfo=UTC),  # Last week of Sept 2026
        rollover_window_days=7,
    )
    intent_4 = OrderIntent(
        signal_id="SIG_1004",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=100, price=Decimal("2500.00"), contract_month="2026-09"),
            OrderLeg(symbol="TCS", side=OrderSide.SELL, quantity=50, price=Decimal("3500.00"), contract_month="2026-09"),
        ],
    )
    res_4 = engine.evaluate(intent_4, context_4)
    print_result(4, "Contract Rollover", res_4)
    assert res_4.outcome == RMSOutcome.PASS
    assert res_4.intent.legs[0].contract_month == "2026-10"

    # ----------------------------------------------------
    # Scenario 5: Strategy Position Limit Reached → REJECT
    # ----------------------------------------------------
    print_separator("5. Strategy Position Limit Reached -> Expected [REJECT]")
    context_5 = RMSContext(
        strategy_configs=configs,
        open_positions={"MODEL_WHITE": 5},  # MODEL_WHITE max_open_positions = 5
    )
    intent_5 = OrderIntent(
        signal_id="SIG_1005",
        strategy_id="MODEL_WHITE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="INFY", side=OrderSide.BUY, quantity=50, price=Decimal("1500.00"), contract_month="2026-10"),
        ],
    )
    res_5 = engine.evaluate(intent_5, context_5)
    print_result(5, "Position Limit Reached", res_5)
    assert res_5.outcome == RMSOutcome.REJECT
    assert res_5.check_number == 7

    # ----------------------------------------------------
    # Scenario 6: Money-Per-Stock Limit Exceeded → REJECT
    # ----------------------------------------------------
    print_separator("6. Money-Per-Stock Limit Exceeded -> Expected [REJECT]")
    context_6 = RMSContext(
        strategy_configs=configs,
        symbol_exposures={"RELIANCE": Decimal(4000000)},  # Existing 40 Lakh
    )
    # Order for 15 Lakh additional notional (40 Lakh + 15 Lakh = 55 Lakh > 50 Lakh limit)
    intent_6 = OrderIntent(
        signal_id="SIG_1006",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(symbol="RELIANCE", side=OrderSide.BUY, quantity=600, price=Decimal("2500.00"), contract_month="2026-10"),
        ],
    )
    res_6 = engine.evaluate(intent_6, context_6)
    print_result(6, "Money-Per-Stock Limit Exceeded", res_6)
    assert res_6.outcome == RMSOutcome.REJECT
    assert res_6.check_number == 8

    print("\n" + "=" * 70)
    print("✨ ALL 6 SCENARIOS EXECUTED SUCCESSFULLY AND PASSED ASSERTIONS!")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
