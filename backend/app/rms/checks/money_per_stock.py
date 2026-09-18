"""CHECK 8 — MONEY PER STOCK check implementation."""

from decimal import Decimal

from app.core.identifiers import normalize_symbol
from app.rms.checks.base import BaseRMSCheck
from app.rms.models import (
    CheckResult,
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    RMSContext,
    RMSOutcome,
    exposure_key,
    signed_quantity,
)


class MoneyPerStockCheck(BaseRMSCheck):
    """CHECK 8 — Enforces money budget per symbol per account/strategy."""

    @property
    def check_number(self) -> int:
        return 8

    @property
    def check_name(self) -> str:
        return "MONEY_PER_STOCK"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        # Position reduction / emergency flatten operations must not be blocked by money budgets
        if intent.action == OrderAction.CLOSE or intent.intent_mode == ExecutionIntentMode.EMERGENCY_FLATTEN:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )
        strategy_cfg = context.strategy_configs.get(intent.strategy_id)
        strategy_limit = (
            strategy_cfg.money_limit_per_symbol if strategy_cfg is not None else None
        )

        # Cancel Exposure ON: opposite-side legs net against existing exposure, so
        # the limit applies to the resulting net position. OFF: gross (unchanged).
        net_basis = (
            intent.account_id is not None
            and intent.account_id in context.cancel_exposure_accounts
        )

        symbol_order_notionals: dict[str, Decimal] = {}
        symbol_order_qty: dict[str, Decimal] = {}
        symbol_order_net_qty: dict[str, Decimal] = {}
        for leg in intent.legs:
            symbol = normalize_symbol(leg.symbol)
            current_notional = symbol_order_notionals.get(symbol, Decimal(0))
            symbol_order_notionals[symbol] = current_notional + leg.effective_notional
            symbol_order_qty[symbol] = symbol_order_qty.get(symbol, Decimal(0)) + abs(
                Decimal(str(leg.quantity))
            )
            symbol_order_net_qty[symbol] = symbol_order_net_qty.get(
                symbol, Decimal(0)
            ) + signed_quantity(leg.side, leg.quantity)

        if not symbol_order_notionals:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        for symbol, order_notional in symbol_order_notionals.items():
            account_limit = None
            if intent.account_id is not None:
                account_limit = context.per_symbol_limits.get((intent.account_id, symbol))
                if account_limit is None:
                    account_limit = context.default_symbol_limits.get(intent.account_id)
            limit_per_symbol = account_limit if account_limit is not None else strategy_limit
            if limit_per_symbol is None or limit_per_symbol <= 0:
                return CheckResult(
                    check_number=self.check_number,
                    check_name=self.check_name,
                    outcome=RMSOutcome.REJECT,
                    reason=f"NO_SYMBOL_LIMIT_CONFIGURED: No per-symbol limit configured for account {intent.account_id} and symbol '{symbol}'",
                )
            if net_basis:
                # Net shares before/after (as the broker nets them), valued at this
                # order's price for the symbol — a flat position is exactly zero.
                existing_qty = context.symbol_net_quantities.get(
                    exposure_key(intent, symbol), Decimal(0)
                )
                qty_after = existing_qty + symbol_order_net_qty[symbol]
                order_qty = symbol_order_qty[symbol]
                ref_price = order_notional / order_qty if order_qty > 0 else Decimal(0)
                net_after = abs(qty_after) * ref_price
                # A trade that shrinks the net position always passes (it reduces
                # risk), even if the account is already above a lowered limit.
                if net_after > limit_per_symbol and abs(qty_after) > abs(existing_qty):
                    return CheckResult(
                        check_number=self.check_number,
                        check_name=self.check_name,
                        outcome=RMSOutcome.REJECT,
                        reason=(
                            f"MONEY_LIMIT_EXCEEDED: Symbol '{symbol}' net exposure of {net_after} "
                            f"(net qty {existing_qty} -> {qty_after} @ {ref_price}) "
                            f"exceeds limit of {limit_per_symbol} [cancel_exposure=ON, net basis]."
                        ),
                    )
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
