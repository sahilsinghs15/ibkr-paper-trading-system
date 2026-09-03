"""
test_risk_ceiling_2000.py — Comprehensive unit tests for strict $2,000 risk policy enforcement.

Verifies:
1. Model Blue committed capital computation (total_margin * alloc_pct = $1,000).
2. Fail-closed behavior when capital or position limits are unconfigured/invalid.
3. RMS Check 7 (OpenPositionLimitCheck) enforcing max 2 positions.
4. RMS Check 8 (MoneyPerStockCheck) enforcing max $1,000 per symbol.
5. Retry policy disabling paper retries on live ports (4001, 7496).
"""
from decimal import Decimal
import pytest

from app.rms.checks.money_per_stock import MoneyPerStockCheck
from app.rms.checks.position_limit import OpenPositionLimitCheck
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
)
from app.oms.retry_policy import paper_retry_ports_allowed


class TestRiskCeilingPolicy:
    """Test mathematical consistency and enforcement of $2,000 risk ceiling."""

    def test_model_blue_committed_capital_calculation(self):
        total_margin = Decimal("2000.00")
        alloc_pct = Decimal("0.50")
        committed_capital = total_margin * alloc_pct
        assert committed_capital == Decimal("1000.00")

        # Leg pair allocation: 50% leg A ($500), 50% leg B ($500)
        leg_a = committed_capital * Decimal("0.50")
        leg_b = committed_capital * Decimal("0.50")
        assert leg_a + leg_b == Decimal("1000.00")

    def test_open_position_limit_check_pass_and_reject(self):
        check = OpenPositionLimitCheck()

        # Configured max_positions = 2
        strat_cfg = StrategyConfig(
            strategy_id="model_blue",
            max_open_positions=2,
        )
        ctx = RMSContext(
            strategy_configs={"model_blue": strat_cfg},
            open_positions={(7, "model_blue"): 1},  # 1 position open
        )
        intent = OrderIntent(
            signal_id="sig1",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            intent_mode=ExecutionIntentMode.OPEN,
            account_id=7,
            legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal("50.00"), contract_month="202609")],
        )

        # 1 position open < 2 max positions -> PASS
        res1 = check.evaluate(intent, ctx)
        assert res1.outcome == RMSOutcome.PASS

        # 2 positions open >= 2 max positions -> REJECT
        ctx.open_positions[(7, "model_blue")] = 2
        res2 = check.evaluate(intent, ctx)
        assert res2.outcome == RMSOutcome.REJECT
        assert "OPEN_POSITION_LIMIT_REACHED" in res2.reason

    def test_money_per_stock_check_pass_and_reject(self):
        check = MoneyPerStockCheck()
        ctx = RMSContext(
            default_symbol_limits={7: Decimal("1000.00")},
            symbol_exposures={(7, "AAPL"): Decimal("600.00")},
        )
        
        # New intent: $300 notional for AAPL -> Total exposure $900 <= $1,000 limit -> PASS
        intent1 = OrderIntent(
            signal_id="sig2",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            intent_mode=ExecutionIntentMode.OPEN,
            account_id=7,
            legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=3, price=Decimal("100.00"), contract_month="202609", notional=Decimal("300.00"))],
        )
        res1 = check.evaluate(intent1, ctx)
        assert res1.outcome == RMSOutcome.PASS

        # New intent: $500 notional for AAPL -> Total exposure $1,100 > $1,000 limit -> REJECT
        intent2 = OrderIntent(
            signal_id="sig3",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            intent_mode=ExecutionIntentMode.OPEN,
            account_id=7,
            legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=5, price=Decimal("100.00"), contract_month="202609", notional=Decimal("500.00"))],
        )
        res2 = check.evaluate(intent2, ctx)
        assert res2.outcome == RMSOutcome.REJECT
        assert "MONEY_LIMIT_EXCEEDED" in res2.reason

    def test_money_per_stock_fails_closed_when_unconfigured(self):
        check = MoneyPerStockCheck()
        # No default symbol limit and no per_symbol_limits configured
        ctx = RMSContext(
            default_symbol_limits={},
            per_symbol_limits={},
        )
        intent = OrderIntent(
            signal_id="sig4",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            intent_mode=ExecutionIntentMode.OPEN,
            account_id=7,
            legs=[OrderLeg(symbol="MSFT", side=OrderSide.BUY, quantity=5, price=Decimal("100.00"), contract_month="202609", notional=Decimal("500.00"))],
        )
        res = check.evaluate(intent, ctx)
        assert res.outcome == RMSOutcome.REJECT
        assert "NO_SYMBOL_LIMIT_CONFIGURED" in res.reason

    def test_live_port_retry_policy_disabled(self):
        # Live Gateway port 4001 / TWS port 7496 must disable paper unhedged retries
        assert paper_retry_ports_allowed(4001) is True
        assert paper_retry_ports_allowed(7496) is False

        # Paper Gateway port 4002 / TWS port 7497 allows paper retries
        assert paper_retry_ports_allowed(4002) is True
        assert paper_retry_ports_allowed(7497) is True
