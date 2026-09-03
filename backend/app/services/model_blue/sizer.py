"""Production Model Blue pair sizer (US ETF/STK).

IBKR API rejects fractional STK orders (error 10243), so STK quantities are
whole shares rounded down so notional never exceeds the allocated target.

Sizing rules:
- exactly two legs
- abs weights must sum to 1.0 (the signal generator normalises them)
- execution side = sign(weight * direction)
- leg target notional = pair_budget * abs(weight)
- pair market value therefore equals pair_budget
- quantity = floor(target_notional / price) whole shares for STK (IBKR API)
- reject if any leg notional is below MIN_ORDER_NOTIONAL

pair_budget is allocations.pair_max_allocation_pct * (total_margin *
alloc_pct), resolved by the account router.
"""

import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.models.signal import Signal
from app.rms.models import OrderSide
from app.services.model_blue.allocation import CommittedCapitalProvider
from app.services.model_blue.parser import (
    ModelBlueValidationError,
    is_model_blue_strategy,
)

logger = logging.getLogger(__name__)

MIN_ORDER_NOTIONAL = Decimal(100)
WEIGHT_SUM_TOLERANCE = Decimal("0.000001")
# Max absolute deviation, in share-of-pair terms, between the realised
# notional split and the intended weight split. 0.05 = five percentage points.
DEFAULT_RATIO_TOLERANCE = Decimal("0.05")
# Minimum fraction of the pair budget that must actually be deployed.
# 0 disables the check.
DEFAULT_MIN_DEPLOYMENT = Decimal(0)
_QTY_QUANTUM = Decimal("0.0001")
_STK_QTY_QUANTUM = Decimal(1)


@dataclass(frozen=True)
class SizedModelBlueLeg:
    """A fully sized Model Blue leg ready to become an OrderIntent leg."""

    symbol: str
    instrument_type: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    notional: Decimal
    weight: float


class ModelBlueSizer:
    """Sizes Model Blue OPEN signals using an injected committed-capital provider."""

    def __init__(
        self,
        committed_capital_provider: CommittedCapitalProvider,
        *,
        min_order_notional: Decimal | None = None,
        ratio_tolerance: Decimal | None = None,
        min_deployment: Decimal | None = None,
    ) -> None:
        self._committed_capital_provider = committed_capital_provider
        self._min_order_notional = (
            min_order_notional if min_order_notional is not None else MIN_ORDER_NOTIONAL
        )
        self._ratio_tolerance = (
            ratio_tolerance if ratio_tolerance is not None else DEFAULT_RATIO_TOLERANCE
        )
        self._min_deployment = (
            min_deployment if min_deployment is not None else DEFAULT_MIN_DEPLOYMENT
        )

    def size_open(
        self,
        signal: Signal,
        *,
        pair_budget: Decimal | None = None,
    ) -> tuple[SizedModelBlueLeg, SizedModelBlueLeg]:
        """Return two sized legs for a validated Model Blue OPEN signal.

        pair_budget is the pair's total market-value budget, resolved by the
        account router as pair_max_allocation_pct * committed. When omitted,
        falls back to the injected committed-capital provider (test path).
        That fallback treats committed as a pair budget, which is only
        correct for tests — production always passes pair_budget.
        """
        if not is_model_blue_strategy(signal.strategy_id):
            raise ModelBlueValidationError(
                "MODEL_BLUE_STRATEGY_MISMATCH: sizer only accepts model_blue."
            )
        if str(signal.action).upper() != "OPEN":
            raise ModelBlueValidationError(
                "MODEL_BLUE_INVALID_ACTION: sizer does not size CLOSE signals."
            )
        if signal.direction not in (1, -1):
            raise ModelBlueValidationError(
                "MODEL_BLUE_INVALID_DIRECTION: direction must be +1 or -1."
            )
        if len(signal.legs) != 2:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_INVALID_LEG_COUNT: OPEN requires exactly 2 legs, got {len(signal.legs)}."
            )

        if pair_budget is not None:
            budget = pair_budget
        else:
            budget = self._committed_capital_provider.get_committed(
                signal.strategy_id or ""
            )
        if budget is None or budget <= 0:
            raise ModelBlueValidationError(
                "MODEL_BLUE_PAIR_BUDGET_NOT_CONFIGURED: pair budget is unset or "
                "non-positive for this account/strategy. Do not invent a "
                "financial amount."
            )

        for index, leg in enumerate(signal.legs):
            if leg.weight is None:
                raise ModelBlueValidationError(
                    f"MODEL_BLUE_MISSING_WEIGHT: legs[{index}] requires a numeric weight."
                )

        weight_sum = sum(
            (Decimal(str(abs(leg.weight))) for leg in signal.legs), Decimal(0)
        )
        if abs(weight_sum - Decimal(1)) > WEIGHT_SUM_TOLERANCE:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_WEIGHT_SUM_INVALID: abs weights sum to {weight_sum}, "
                f"expected 1.0 (+/- {WEIGHT_SUM_TOLERANCE}). Sizing is "
                "leg = abs(weight) * pair_budget, so weights that do not sum to 1 "
                "would deploy more or less than the pair budget."
            )

        sized = tuple(
            self._size_leg(
                symbol=leg.symbol,
                instrument_type=leg.instrument_type,
                weight=leg.weight,
                price=leg.price,
                direction=signal.direction,
                target_notional=budget * Decimal(str(abs(leg.weight))),
            )
            for leg in signal.legs
        )

        logger.info(
            "Model Blue size_open: trade_id=%s pair_budget=%s weight_sum=%s legs=%s",
            signal.trade_id or signal.signal_id,
            budget,
            weight_sum,
            [
                (leg.symbol, leg.side.value, str(leg.quantity), str(leg.notional))
                for leg in sized
            ],
        )
        self._validate_realised_ratio(
            sized, budget=budget, trade_id=signal.trade_id or signal.signal_id or ""
        )
        return sized

    def _validate_realised_ratio(
        self,
        sized: tuple[SizedModelBlueLeg, ...],
        *,
        budget: Decimal,
        trade_id: str,
    ) -> None:
        """Reject a pair whose realised notional split drifted from its weights.

        Whole-share ROUND_DOWN is applied per leg independently, so the
        realised split is not the weight split. At large notionals the error is
        negligible; at small ones it is not. A 0.5/0.5 signal that lands as
        $13 vs $24 is a ~2:1 directional bet, not a hedged pair, and every
        downstream component -- pair P&L, the exit levels, the reconciler --
        assumes the ratio the signal asked for.

        Compares dimensionless shares of the pair rather than a leg-vs-leg
        ratio, so this generalises to N legs and cannot divide by zero.
        """
        total_notional = sum((leg.notional for leg in sized), Decimal(0))
        if total_notional <= 0:
            raise ModelBlueValidationError(
                "MODEL_BLUE_ZERO_NOTIONAL: pair sized to zero total notional."
            )

        weight_sum = sum(
            (Decimal(str(abs(leg.weight))) for leg in sized), Decimal(0)
        )
        worst_symbol = ""
        worst_dev = Decimal(0)
        detail: list[str] = []
        for leg in sized:
            realised_share = leg.notional / total_notional
            intended_share = Decimal(str(abs(leg.weight))) / weight_sum
            dev = abs(realised_share - intended_share)
            detail.append(
                f"{leg.symbol}: intended {intended_share:.4f} "
                f"realised {realised_share:.4f} dev {dev:.4f}"
            )
            if dev > worst_dev:
                worst_dev = dev
                worst_symbol = leg.symbol

        deployment = total_notional / budget

        logger.info(
            "Model Blue ratio check: trade_id=%s budget=%s deployed=%s "
            "(%.1f%%) worst_dev=%.4f tolerance=%s | %s",
            trade_id,
            budget,
            total_notional,
            deployment * 100,
            worst_dev,
            self._ratio_tolerance,
            "; ".join(detail),
        )

        if worst_dev > self._ratio_tolerance:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_RATIO_DRIFT: after whole-share rounding, "
                f"{worst_symbol} deviates {worst_dev:.4f} from its intended "
                f"share of the pair (tolerance {self._ratio_tolerance}). "
                f"Realised split: {'; '.join(detail)}. Raise "
                "pair_max_allocation_pct so rounding error is proportionally "
                "smaller, or widen PAIR_RATIO_TOLERANCE."
            )

        if self._min_deployment > 0 and deployment < self._min_deployment:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_UNDER_DEPLOYED: rounding left {total_notional} of "
                f"a {budget} pair budget deployed ({deployment:.2%}), below the "
                f"{self._min_deployment:.2%} floor."
            )

    def _size_leg(
        self,
        *,
        symbol: str,
        instrument_type: str,
        weight: float,
        price: Decimal,
        direction: int,
        target_notional: Decimal,
    ) -> SizedModelBlueLeg:
        if price <= 0:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_INVALID_PRICE: {symbol} price must be positive."
            )
        if (instrument_type or "STK").upper() in ("STK", "ETF"):
            quantity = (target_notional / price).quantize(
                _STK_QTY_QUANTUM, rounding=ROUND_DOWN
            )
            if quantity < 1:
                raise ModelBlueValidationError(
                    f"MODEL_BLUE_MIN_SHARE: {symbol} sizes below 1 share at price {price}."
                )
        else:
            quantity = (target_notional / price).quantize(
                _QTY_QUANTUM, rounding=ROUND_HALF_UP
            )
        notional = quantity * price
        if notional < target_notional:
            logger.info(
                "MODEL_BLUE_QTY_ROUNDED: symbol=%s qty=%s price=%s notional=%s target=%s",
                symbol,
                quantity,
                price,
                notional,
                target_notional,
            )
        if notional < self._min_order_notional:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_MIN_NOTIONAL: {symbol} notional {notional} is below "
                f"minimum {self._min_order_notional}."
            )
        signed = weight * direction
        if signed == 0:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_INVALID_SIDE: {symbol} weight * direction is zero."
            )
        side = OrderSide.BUY if signed > 0 else OrderSide.SELL
        return SizedModelBlueLeg(
            symbol=symbol,
            instrument_type=instrument_type,
            side=side,
            quantity=quantity,
            price=price,
            notional=notional,
            weight=weight,
        )
