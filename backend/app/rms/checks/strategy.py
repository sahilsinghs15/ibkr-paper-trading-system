"""CHECK 3 — STRATEGY check implementation."""

from app.rms.checks.base import BaseRMSCheck
from app.rms.models import (
    CheckResult,
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    RMSContext,
    RMSOutcome,
)


class StrategyCheck(BaseRMSCheck):
    """CHECK 3 — Validates that strategy attribution is present and configured in RMS context."""

    @property
    def check_number(self) -> int:
        return 3

    @property
    def check_name(self) -> str:
        return "STRATEGY"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        # Emergency flatten and close operations must not be blocked by disabled/missing strategy configs
        if intent.action == OrderAction.CLOSE or intent.intent_mode == ExecutionIntentMode.EMERGENCY_FLATTEN:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        if not intent.strategy_id or not intent.strategy_id.strip():
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason="MISSING_STRATEGY_ID: OrderIntent does not contain a strategy attribution.",
            )

        if intent.strategy_id not in context.strategy_configs:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=f"UNKNOWN_STRATEGY: Strategy '{intent.strategy_id}' is not configured in RMS context.",
            )

        return CheckResult(
            check_number=self.check_number,
            check_name=self.check_name,
            outcome=RMSOutcome.PASS,
        )
