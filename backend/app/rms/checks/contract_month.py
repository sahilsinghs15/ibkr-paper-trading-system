"""CHECK 4 — CONTRACT MONTH check implementation."""

from datetime import datetime

from app.rms.checks.base import BaseRMSCheck
from app.rms.models import (
    CheckResult,
    OrderAction,
    OrderIntent,
    OrderLeg,
    RMSContext,
    RMSOutcome,
)


def get_next_contract_month(current_contract_month: str) -> str:
    """Calculate the next contract month string formatted as 'YYYY-MM'.

    Args:
        current_contract_month: Contract month in 'YYYY-MM' format (e.g., '2026-09').

    Returns:
        Next contract month string (e.g., '2026-10').
    """
    try:
        parts = current_contract_month.split("-")
        year, month = int(parts[0]), int(parts[1])
        if month == 12:
            return f"{year + 1:04d}-01"
        return f"{year:04d}-{month + 1:02d}"
    except (ValueError, IndexError):
        return current_contract_month


def is_default_rollover_active(
    contract_month: str, current_time: datetime, window_days: int
) -> bool:
    """Default rollover check based on proximity to contract month end.

    Determines if current_time is within window_days of the end of the contract_month.
    """
    try:
        parts = contract_month.split("-")
        year, month = int(parts[0]), int(parts[1])
        # Approximate end of month as 28th-31st
        if month == 12:
            next_month_start = datetime(year + 1, 1, 1, tzinfo=current_time.tzinfo)
        else:
            next_month_start = datetime(year, month + 1, 1, tzinfo=current_time.tzinfo)
        days_remaining = (next_month_start - current_time).days
        return 0 <= days_remaining <= window_days
    except (ValueError, TypeError, OverflowError):
        return False


class ContractMonthCheck(BaseRMSCheck):
    """CHECK 4 — Validates and adjusts contract month during rollover periods for OPEN entries."""

    @property
    def check_number(self) -> int:
        return 4

    @property
    def check_name(self) -> str:
        return "CONTRACT_MONTH"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        if not intent.legs:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        # Check if leg contract months are mismatched
        first_month = intent.legs[0].contract_month
        mismatched_legs = any(leg.contract_month != first_month for leg in intent.legs)

        # Check rollover condition for OPEN intents
        should_rollover = False
        if intent.action == OrderAction.OPEN:
            if context.rollover_checker is not None:
                should_rollover = context.rollover_checker(
                    first_month, context.current_time, context.rollover_window_days
                )
            elif context.target_rollover_month is not None:
                should_rollover = True
            else:
                should_rollover = is_default_rollover_active(
                    first_month, context.current_time, context.rollover_window_days
                )

        if not should_rollover and not mismatched_legs:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        # Determine target contract month
        if context.target_rollover_month is not None:
            target_month = context.target_rollover_month
        elif should_rollover:
            target_month = get_next_contract_month(first_month)
        else:
            target_month = first_month

        # Construct adjusted legs with target contract month
        adjusted_legs: list[OrderLeg] = []
        for leg in intent.legs:
            adjusted_legs.append(
                OrderLeg(
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=leg.quantity,
                    price=leg.price,
                    contract_month=target_month,
                    con_id=leg.con_id,
                    notional=leg.notional,
                )
            )

        adjusted_intent = OrderIntent(
            signal_id=intent.signal_id,
            strategy_id=intent.strategy_id,
            action=intent.action,
            legs=adjusted_legs,
            account_id=intent.account_id,
            timestamp=intent.timestamp,
        )

        reason_msg = (
            f"CONTRACT_MONTH_ROLLOVER: Adjusted contract month from '{first_month}' "
            f"to '{target_month}' for both legs."
        )

        return CheckResult(
            check_number=self.check_number,
            check_name=self.check_name,
            outcome=RMSOutcome.ADJUST,
            reason=reason_msg,
            adjusted_intent=adjusted_intent,
        )
