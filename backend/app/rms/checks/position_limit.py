"""CHECK 7 — OPEN POSITION LIMIT check implementation."""

from app.rms.checks.base import BaseRMSCheck
from app.rms.models import (
    CheckResult,
    OrderAction,
    OrderIntent,
    RMSContext,
    RMSOutcome,
    open_position_key,
)


class OpenPositionLimitCheck(BaseRMSCheck):
    """CHECK 7 — Enforces configurable maximum open position limit per strategy for OPEN signals."""

    @property
    def check_number(self) -> int:
        return 7

    @property
    def check_name(self) -> str:
        return "OPEN_POSITION_LIMIT"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        # CLOSE signals must not be blocked by open position limits
        if intent.action == OrderAction.CLOSE:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        strategy_cfg = context.strategy_configs.get(intent.strategy_id)
        if strategy_cfg is None:
            # Note: Strategy existence is validated by Check 3. If missing here, fail safe.
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=f"MISSING_STRATEGY_CONFIG: Configuration for strategy '{intent.strategy_id}' not found.",
            )

        current_positions = context.open_positions.get(open_position_key(intent), 0)
        max_positions = strategy_cfg.max_open_positions
        if intent.account_id is not None:
            account_limit = context.account_open_limits.get(
                (intent.account_id, intent.strategy_id)
            )
            if account_limit is not None:
                max_positions = account_limit

        if max_positions is None or max_positions <= 0:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=f"INVALID_MAX_POSITIONS_LIMIT: Max open positions for strategy '{intent.strategy_id}' is missing or invalid ({max_positions}).",
            )

        if current_positions >= max_positions:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=(
                    f"OPEN_POSITION_LIMIT_REACHED: Strategy '{intent.strategy_id}' "
                    f"has {current_positions} open position(s), meeting or exceeding limit of {max_positions}."
                ),
            )

        return CheckResult(
            check_number=self.check_number,
            check_name=self.check_name,
            outcome=RMSOutcome.PASS,
        )
