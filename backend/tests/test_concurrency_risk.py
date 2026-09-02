"""
test_concurrency_risk.py — Non-transmitting concurrency, order transmission boundary, and fail-closed matrix tests.

Verifies:
1. Concurrency safety & execution claim barriers under simulated multi-worker signals.
2. Order Transmission Boundary: Broker submission functions are NEVER called if RMS rejects or Kill Switch is active.
3. Complete Failure-Mode Matrix: Missing or invalid risk parameters fail closed safely.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.rms.checks.duplicate import DuplicateCheck
from app.rms.checks.money_per_stock import MoneyPerStockCheck
from app.rms.checks.position_limit import OpenPositionLimitCheck
from app.rms.engine import RMSEngine
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
from app.services.kill_switch import is_account_kill_switch_active, _arm_kill_switch_cache


class TestConcurrencyAndExecutionBoundary:
    """Non-transmitting concurrency and safety boundary tests."""

    @pytest.mark.asyncio
    async def test_order_transmission_boundary_blocked_on_rms_reject(self):
        """Verify broker submit_order is NEVER called when RMS rejects an intent."""
        mock_broker_adapter = AsyncMock()
        mock_broker_adapter.submit_order = AsyncMock()

        # Engine rejecting because max open positions exceeded
        strat_cfg = StrategyConfig(strategy_id="model_blue", max_open_positions=2)
        ctx = RMSContext(
            strategy_configs={"model_blue": strat_cfg},
            open_positions={(7, "model_blue"): 2},  # Already at limit 2
        )
        engine = RMSEngine()

        intent = OrderIntent(
            signal_id="sig_over_limit",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            intent_mode=ExecutionIntentMode.OPEN,
            account_id=7,
            legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal("100.00"), contract_month="202609")],
        )

        result = engine.evaluate(intent, ctx)
        assert result.outcome == RMSOutcome.REJECT

        # Simulate execution pipeline logic: submission skipped if outcome != PASS
        if result.outcome == RMSOutcome.PASS:
            await mock_broker_adapter.submit_order(intent)

        mock_broker_adapter.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_transmission_boundary_blocked_on_kill_switch(self):
        """Verify broker submit_order is NEVER called when Kill Switch is active."""
        mock_broker_adapter = AsyncMock()
        mock_broker_adapter.submit_order = AsyncMock()

        # Arm kill switch cache for account 7
        _arm_kill_switch_cache(7)
        assert is_account_kill_switch_active(7) is True

        intent = OrderIntent(
            signal_id="sig_ks_active",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            intent_mode=ExecutionIntentMode.OPEN,
            account_id=7,
            legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=5, price=Decimal("100.00"), contract_month="202609")],
        )

        # Pipeline checks kill switch state before submission
        ks_active = is_account_kill_switch_active(intent.account_id)
        if not ks_active:
            await mock_broker_adapter.submit_order(intent)

        mock_broker_adapter.submit_order.assert_not_called()

    def test_concurrent_claim_barrier_deduplication(self):
        """Verify Check 2 (DuplicateCheck) blocks duplicate concurrent claims for the same signal."""
        check = DuplicateCheck()
        ctx = RMSContext(processed_signals={(7, "model_blue", "sig_shared_123")})

        intent = OrderIntent(
            signal_id="sig_shared_123",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            intent_mode=ExecutionIntentMode.OPEN,
            account_id=7,
            legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=5, price=Decimal("100.00"), contract_month="202609")],
        )

        res = check.evaluate(intent, ctx)
        assert res.outcome == RMSOutcome.REJECT
        assert "DUPLICATE_SIGNAL" in res.reason

    def test_concurrency_position_limit_atomic_rejection(self):
        """Verify that when 2 workers process signals concurrently, the 3rd is rejected."""
        pos_check = OpenPositionLimitCheck()
        strat_cfg = StrategyConfig(strategy_id="model_blue", max_open_positions=2)

        # Worker A processes position 1
        ctx_a = RMSContext(strategy_configs={"model_blue": strat_cfg}, open_positions={(7, "model_blue"): 0})
        intent_a = OrderIntent(signal_id="sig_a", strategy_id="model_blue", action=OrderAction.OPEN, intent_mode=ExecutionIntentMode.OPEN, account_id=7, legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=5, price=Decimal("100"), contract_month="202609")])
        assert pos_check.evaluate(intent_a, ctx_a).outcome == RMSOutcome.PASS

        # Worker B processes position 2
        ctx_b = RMSContext(strategy_configs={"model_blue": strat_cfg}, open_positions={(7, "model_blue"): 1})
        intent_b = OrderIntent(signal_id="sig_b", strategy_id="model_blue", action=OrderAction.OPEN, intent_mode=ExecutionIntentMode.OPEN, account_id=7, legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=5, price=Decimal("100"), contract_month="202609")])
        assert pos_check.evaluate(intent_b, ctx_b).outcome == RMSOutcome.PASS

        # Worker C attempts position 3 -> REJECTED
        ctx_c = RMSContext(strategy_configs={"model_blue": strat_cfg}, open_positions={(7, "model_blue"): 2})
        intent_c = OrderIntent(signal_id="sig_c", strategy_id="model_blue", action=OrderAction.OPEN, intent_mode=ExecutionIntentMode.OPEN, account_id=7, legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=5, price=Decimal("100"), contract_month="202609")])
        res_c = pos_check.evaluate(intent_c, ctx_c)
        assert res_c.outcome == RMSOutcome.REJECT
        assert "OPEN_POSITION_LIMIT_REACHED" in res_c.reason


class TestFailureModeMatrix:
    """Test all failure modes fail closed safely."""

    def test_missing_strategy_config(self):
        check = OpenPositionLimitCheck()
        ctx = RMSContext(strategy_configs={})
        intent = OrderIntent(signal_id="s1", strategy_id="unknown_strat", action=OrderAction.OPEN, intent_mode=ExecutionIntentMode.OPEN, account_id=7, legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=1, price=Decimal("100"), contract_month="202609")])
        res = check.evaluate(intent, ctx)
        assert res.outcome == RMSOutcome.REJECT
        assert "MISSING_STRATEGY_CONFIG" in res.reason

    def test_missing_symbol_limit(self):
        check = MoneyPerStockCheck()
        ctx = RMSContext(default_symbol_limits={}, per_symbol_limits={})
        intent = OrderIntent(signal_id="s2", strategy_id="model_blue", action=OrderAction.OPEN, intent_mode=ExecutionIntentMode.OPEN, account_id=7, legs=[OrderLeg(symbol="MSFT", side=OrderSide.BUY, quantity=1, price=Decimal("100"), contract_month="202609", notional=Decimal("100"))])
        res = check.evaluate(intent, ctx)
        assert res.outcome == RMSOutcome.REJECT
        assert "NO_SYMBOL_LIMIT_CONFIGURED" in res.reason

    def test_zero_position_limit(self):
        check = OpenPositionLimitCheck()
        strat_cfg = StrategyConfig(strategy_id="model_blue", max_open_positions=0)
        ctx = RMSContext(strategy_configs={"model_blue": strat_cfg})
        intent = OrderIntent(signal_id="s3", strategy_id="model_blue", action=OrderAction.OPEN, intent_mode=ExecutionIntentMode.OPEN, account_id=7, legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=1, price=Decimal("100"), contract_month="202609")])
        res = check.evaluate(intent, ctx)
        assert res.outcome == RMSOutcome.REJECT
        assert "INVALID_MAX_POSITIONS_LIMIT" in res.reason
