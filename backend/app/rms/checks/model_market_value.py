"""CHECK 101 — MODEL MARKET VALUE cap (local extension, not one of the brief's nine).

Bounds total market value of one model's open positions on one account to:

    total_margin * alloc_pct * market_value_utilisation_cap

Individual pair size is bounded separately at sizing time by
pair_max_allocation_pct. This check stops N pairs from collectively
overrunning the model's allocation.

Market value, not margin: gross abs(notional) summed across legs, never
netted. Nothing here estimates IBKR collateral requirements.

Seeded from `positions` (which carries strategy_id), not `broker_positions`
(which does not). Exposure the strategy path never created is invisible here
by design.
"""

from decimal import Decimal

from app.rms.checks.base import BaseRMSCheck
from app.rms.market_value import intent_market_value
from app.rms.models import (
    CheckResult,
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    RMSContext,
    RMSOutcome,
    model_value_key,
)


class ModelMarketValueCheck(BaseRMSCheck):
    """CHECK 101 — Enforces per-account-per-model market-value headroom."""

    @property
    def check_number(self) -> int:
        return 101

    @property
    def check_name(self) -> str:
        return "MODEL_MARKET_VALUE"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        if not context.market_value_check_enabled:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
                reason=(
                    "MARKET_VALUE_CHECK_DISABLED: shadow mode, see "
                    "MARKET_VALUE_CHECK_ENABLED."
                ),
            )

        # Reductions and emergency flatten free exposure rather than consume
        # it. Blocking them at a ceiling would make a full model impossible to
        # de-risk -- the exact opposite of the check's purpose.
        if (
            intent.action == OrderAction.CLOSE
            or intent.intent_mode == ExecutionIntentMode.EMERGENCY_FLATTEN
        ):
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        key = model_value_key(intent)
        if key is None:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
                reason="NO_ACCOUNT_SCOPE: intent has no account_id; no allocation applies.",
            )

        limit = context.model_value_limit.get(key)
        if limit is None or limit <= 0:
            # Fail closed: a missing ceiling is a wiring fault, not permission
            # to trade unbounded.
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=(
                    f"MODEL_VALUE_LIMIT_UNKNOWN: no market-value ceiling published "
                    f"for account_id={key[0]} strategy_id={key[1]}."
                ),
            )

        required = intent_market_value(intent)
        if required <= 0:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        used = context.model_value_used.get(key, Decimal(0))
        projected = used + required

        if projected > limit:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=(
                    f"MODEL_VALUE_EXCEEDED: account_id={key[0]} "
                    f"strategy_id={key[1]} projected market value {projected} "
                    f"(open {used} + new {required}) exceeds model ceiling {limit}."
                ),
            )

        return CheckResult(
            check_number=self.check_number,
            check_name=self.check_name,
            outcome=RMSOutcome.PASS,
        )
