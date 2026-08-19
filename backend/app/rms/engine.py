"""RMS Engine implementation orchestrating sequential check evaluation."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from app.rms.checks.base import BaseRMSCheck
from app.rms.checks.contract_month import ContractMonthCheck
from app.rms.checks.duplicate import DuplicateCheck
from app.rms.checks.money_per_stock import MoneyPerStockCheck
from app.rms.checks.position_limit import OpenPositionLimitCheck
from app.rms.checks.strategy import StrategyCheck
from app.rms.models import CheckResult, OrderIntent, RMSContext, RMSOutcome, RMSResult

logger = logging.getLogger(__name__)


def get_default_checks() -> list[BaseRMSCheck]:
    """Get the default sequence of RMS checks in mandatory execution order.

    Order:
        1. Check 2  — DUPLICATE
        2. Check 3  — STRATEGY
        3. Check 4  — CONTRACT MONTH
        4. Check 7  — OPEN-POSITION LIMIT
        5. Check 8  — MONEY PER STOCK
    """
    return [
        DuplicateCheck(),
        StrategyCheck(),
        ContractMonthCheck(),
        OpenPositionLimitCheck(),
        MoneyPerStockCheck(),
    ]


class RMSEngine:
    """Orchestrates sequential evaluation of RMS checks against an OrderIntent."""

    def __init__(self, checks: Sequence[BaseRMSCheck] | None = None) -> None:
        """Initialize RMSEngine.

        Args:
            checks: Custom sequence of RMS checks. If None, uses default 5 checks.
        """
        self.checks: list[BaseRMSCheck] = list(checks) if checks is not None else get_default_checks()

    def evaluate(self, order_intent: OrderIntent, context: RMSContext) -> RMSResult:
        """Evaluate an OrderIntent through the RMS check pipeline.

        Execution rules:
        1. Checks execute in strict fixed order.
        2. If a check returns ADJUST, the adjusted intent replaces current_intent for later checks.
        3. If a check returns REJECT or HALT, evaluation short-circuits immediately.
        4. Returns RMSResult with final status and full audit trail.

        Args:
            order_intent: The input OrderIntent to evaluate.
            context: The RMSContext containing state and rules.

        Returns:
            An RMSResult instance containing the final outcome and audit details.
        """
        current_intent = order_intent
        check_results: list[CheckResult] = []
        action = (
            order_intent.action.value
            if hasattr(order_intent.action, "value")
            else str(order_intent.action)
        )
        logger.info(
            "RMS evaluate start: signal_id=%s strategy_id=%s action=%s account_id=%s legs=%d",
            order_intent.signal_id,
            order_intent.strategy_id,
            action,
            order_intent.account_id,
            len(order_intent.legs),
        )

        for check in self.checks:
            result = check.evaluate(current_intent, context)
            check_results.append(result)
            logger.info(
                "RMS check %s (%s): outcome=%s reason=%s",
                result.check_number,
                result.check_name,
                result.outcome.value,
                result.reason,
            )

            if result.outcome in (RMSOutcome.REJECT, RMSOutcome.HALT):
                logger.info(
                    "RMS evaluate final: outcome=%s check=%s reason=%s signal_id=%s",
                    result.outcome.value,
                    result.check_number,
                    result.reason,
                    order_intent.signal_id,
                )
                return RMSResult(
                    outcome=result.outcome,
                    intent=current_intent,
                    original_intent=order_intent,
                    check_number=result.check_number,
                    reason=result.reason,
                    check_results=check_results,
                    timestamp=datetime.now(UTC),
                )

            if result.outcome == RMSOutcome.ADJUST and result.adjusted_intent is not None:
                current_intent = result.adjusted_intent

        logger.info(
            "RMS evaluate final: outcome=PASS signal_id=%s checks=%d",
            order_intent.signal_id,
            len(check_results),
        )
        return RMSResult(
            outcome=RMSOutcome.PASS,
            intent=current_intent,
            original_intent=order_intent,
            check_results=check_results,
            timestamp=datetime.now(UTC),
        )
