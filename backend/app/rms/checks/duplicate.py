"""CHECK 2 — DUPLICATE check implementation."""

from app.rms.checks.base import BaseRMSCheck
from app.rms.models import CheckResult, OrderAction, OrderIntent, RMSContext, RMSOutcome


class DuplicateCheck(BaseRMSCheck):
    """CHECK 2 — Identifies duplicate signals based on strategy_id + signal_id for OPEN intents."""

    @property
    def check_number(self) -> int:
        return 2

    @property
    def check_name(self) -> str:
        return "DUPLICATE"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        # CLOSE signals must bypass the duplicate rejection rule
        if intent.action == OrderAction.CLOSE:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        lookup_key = (intent.strategy_id, intent.signal_id)
        if intent.account_id is not None:
            lookup_key = (intent.account_id, intent.strategy_id, intent.signal_id)
        if lookup_key in context.processed_signals:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=f"DUPLICATE_SIGNAL: Signal '{intent.signal_id}' for strategy '{intent.strategy_id}' has already been processed.",
            )

        return CheckResult(
            check_number=self.check_number,
            check_name=self.check_name,
            outcome=RMSOutcome.PASS,
        )
