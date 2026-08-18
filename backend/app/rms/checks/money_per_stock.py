"""CHECK 8 — MONEY PER STOCK check implementation."""

from decimal import Decimal

from app.rms.checks.base import BaseRMSCheck
from app.rms.models import CheckResult, OrderIntent, RMSContext, RMSOutcome, exposure_key


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
        strategy_limit = (
            strategy_cfg.money_limit_per_symbol if strategy_cfg is not None else None
        )

        symbol_order_notionals: dict[str, Decimal] = {}
        for leg in intent.legs:
            current_notional = symbol_order_notionals.get(leg.symbol, Decimal(0))
            symbol_order_notionals[leg.symbol] = current_notional + leg.effective_notional

        if not symbol_order_notionals:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        any_limit = False
        for symbol, order_notional in symbol_order_notionals.items():
            account_limit = None
            if intent.account_id is not None:
                account_limit = context.per_symbol_limits.get((intent.account_id, symbol))
            limit_per_symbol = account_limit if account_limit is not None else strategy_limit
            if limit_per_symbol is None:
                continue
            existing_exposure = context.symbol_exposures.get(
                exposure_key(intent, symbol), Decimal(0)
            )
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
