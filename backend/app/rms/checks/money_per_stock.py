"""CHECK 8 — MONEY PER STOCK check implementation."""

from decimal import Decimal

from app.rms.checks.base import BaseRMSCheck
from app.rms.models import CheckResult, OrderIntent, RMSContext, RMSOutcome


class MoneyPerStockCheck(BaseRMSCheck):
    """CHECK 8 — Enforces money budget per symbol per account/strategy."""

    @property
    def check_number(self) -> int:
        return 8

    @property
    def check_name(self) -> str:
        return "MONEY_PER_STOCK"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        strategy_cfg = context.strategy_configs.get(intent.strategy_id)
        limit_per_symbol = (
            strategy_cfg.money_limit_per_symbol if strategy_cfg is not None else None
        )

        if limit_per_symbol is None:
            # If no limit is configured, check passes
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        # Aggregate required notional per symbol from order legs
        symbol_order_notionals: dict[str, Decimal] = {}
        for leg in intent.legs:
            current_notional = symbol_order_notionals.get(leg.symbol, Decimal(0))
            symbol_order_notionals[leg.symbol] = current_notional + leg.effective_notional

        for symbol, order_notional in symbol_order_notionals.items():
            existing_exposure = context.symbol_exposures.get(symbol, Decimal(0))
            total_exposure = existing_exposure + order_notional

            if total_exposure > limit_per_symbol:
                return CheckResult(
                    check_number=self.check_number,
                    check_name=self.check_name,
                    outcome=RMSOutcome.REJECT,
                    reason=(
                        f"MONEY_LIMIT_EXCEEDED: Symbol '{symbol}' total exposure of {total_exposure} "
                        f"(existing {existing_exposure} + new {order_notional}) exceeds limit of {limit_per_symbol}."
                    ),
                )

        return CheckResult(
            check_number=self.check_number,
            check_name=self.check_name,
            outcome=RMSOutcome.PASS,
        )
