"""Production Model Blue pair sizer (US ETF/STK).

IBKR API rejects fractional STK orders (error 10243), so STK quantities are
whole shares rounded down so notional never exceeds the allocated target.

Reimplements the intended Model Blue pair rules in-process:
- exactly two legs; first leg is the capital anchor
- execution side = sign(weight * direction)
- base target notional = committed
- other target notional = committed * abs(weight) / abs(base_weight)
- quantity = floor(target_notional / price) whole shares for STK (IBKR API)
- reject if any leg notional is below MIN_ORDER_NOTIONAL

Does not import or wrap the reference helper file.
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
_QTY_QUANTUM = Decimal("0.0001")
_STK_QTY_QUANTUM = Decimal("1")


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

    def __init__(self, committed_capital_provider: CommittedCapitalProvider) -> None:
        self._committed_capital_provider = committed_capital_provider

    def size_open(self, signal: Signal) -> tuple[SizedModelBlueLeg, SizedModelBlueLeg]:
        """Return two sized legs for a validated Model Blue OPEN signal."""
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

        committed = self._committed_capital_provider.get_committed(
            signal.strategy_id or ""
        )
        if committed is None or committed <= 0:
            raise ModelBlueValidationError(
                "MODEL_BLUE_COMMITTED_NOT_CONFIGURED: temporary paper committed "
                "notional is unset. Set MODEL_BLUE_COMMITTED_NOTIONAL or inject a "
                "CommittedCapitalProvider. Do not invent a financial amount."
            )

        base = signal.legs[0]
        hedge = signal.legs[1]
        if base.weight is None or hedge.weight is None:
            raise ModelBlueValidationError(
                "MODEL_BLUE_MISSING_WEIGHT: both Model Blue legs require numeric weight."
            )
        base_weight = abs(base.weight)
        if base_weight == 0:
            raise ModelBlueValidationError(
                "MODEL_BLUE_INVALID_WEIGHT: base (first) leg weight must be non-zero."
            )

        sized_base = self._size_leg(
            symbol=base.symbol,
            instrument_type=base.instrument_type,
            weight=base.weight,
            price=base.price,
            direction=signal.direction,
            target_notional=committed,
        )
        sized_hedge = self._size_leg(
            symbol=hedge.symbol,
            instrument_type=hedge.instrument_type,
            weight=hedge.weight,
            price=hedge.price,
            direction=signal.direction,
            target_notional=committed * Decimal(str(abs(hedge.weight))) / Decimal(str(base_weight)),
        )
        logger.info(
            "Model Blue size_open: trade_id=%s committed=%s legs=[(%s %s qty=%s notional=%s), (%s %s qty=%s notional=%s)]",
            signal.trade_id or signal.signal_id,
            committed,
            sized_base.symbol,
            sized_base.side.value,
            sized_base.quantity,
            sized_base.notional,
            sized_hedge.symbol,
            sized_hedge.side.value,
            sized_hedge.quantity,
            sized_hedge.notional,
        )
        return sized_base, sized_hedge

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
        if notional < MIN_ORDER_NOTIONAL:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_MIN_NOTIONAL: {symbol} notional {notional} is below "
                f"minimum {MIN_ORDER_NOTIONAL}."
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
