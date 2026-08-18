"""Parse real TradingView Model Blue webhook payloads into domain Signals."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.signal import Signal, SignalLeg, SignalType

MODEL_BLUE_STRATEGY_ID = "model_blue"


class ModelBlueValidationError(ValueError):
    """Raised when a Model Blue payload cannot be executed as-specified."""


def is_model_blue_strategy(strategy_id: str | None) -> bool:
    """Return True when strategy identity is Model Blue (case-insensitive)."""
    return (strategy_id or "").strip().lower() == MODEL_BLUE_STRATEGY_ID


def parse_model_blue_payload(
    payload: dict[str, Any],
    *,
    timestamp: datetime,
    reason: str,
    raw_payload: dict[str, Any] | None = None,
) -> Signal:
    """Build a domain Signal from a real Model Blue TradingView JSON object.

    OPEN requires exactly two buckets/legs with symbol, weight, and price.
    CLOSE requires trade_id and must not be sized from the payload.
    """
    strategy_id = str(payload.get("strategy") or "").strip()
    if not is_model_blue_strategy(strategy_id):
        raise ModelBlueValidationError(
            f"MODEL_BLUE_STRATEGY_MISMATCH: strategy '{strategy_id}' is not model_blue."
        )

    action = str(payload.get("action") or "").strip().upper()
    if action not in ("OPEN", "CLOSE"):
        raise ModelBlueValidationError(
            f"MODEL_BLUE_INVALID_ACTION: action must be OPEN or CLOSE, got '{action}'."
        )

    trade_id = str(payload.get("trade_id") or "").strip()
    if not trade_id:
        raise ModelBlueValidationError("MODEL_BLUE_MISSING_TRADE_ID: trade_id is required.")

    direction = _parse_direction(payload.get("direction"))
    market = str(payload.get("market") or "").strip() or None

    if action == "CLOSE":
        return Signal(
            signal_type=SignalType.SELL,
            timestamp=timestamp,
            reason=reason,
            signal_id=trade_id,
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            action="CLOSE",
            trade_id=trade_id,
            direction=direction,
            market=market,
            legs=(),
            raw_payload=raw_payload,
        )

    legs = _parse_open_legs(payload.get("buckets"))
    return Signal(
        signal_type=SignalType.BUY,
        timestamp=timestamp,
        reason=reason,
        signal_id=trade_id,
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        action="OPEN",
        trade_id=trade_id,
        direction=direction,
        market=market,
        legs=legs,
        raw_payload=raw_payload,
    )


def _parse_direction(raw: Any) -> int:
    try:
        direction = int(raw)
    except (TypeError, ValueError):
        raise ModelBlueValidationError(
            f"MODEL_BLUE_INVALID_DIRECTION: direction must be +1 or -1, got {raw!r}."
        ) from None
    if direction not in (1, -1):
        raise ModelBlueValidationError(
            f"MODEL_BLUE_INVALID_DIRECTION: direction must be +1 or -1, got {raw!r}."
        )
    return direction


def _parse_open_legs(buckets: Any) -> tuple[SignalLeg, ...]:
    if not isinstance(buckets, list):
        raise ModelBlueValidationError(
            "MODEL_BLUE_MISSING_BUCKETS: OPEN requires buckets with exactly two legs."
        )
    if len(buckets) != 2:
        raise ModelBlueValidationError(
            f"MODEL_BLUE_INVALID_LEG_COUNT: OPEN requires exactly 2 buckets, got {len(buckets)}."
        )

    parsed: list[SignalLeg] = []
    for index, bucket in enumerate(buckets):
        if not isinstance(bucket, dict):
            raise ModelBlueValidationError(
                f"MODEL_BLUE_INVALID_BUCKET: buckets[{index}] must be an object."
            )
        bucket_legs = bucket.get("legs")
        if not isinstance(bucket_legs, list) or len(bucket_legs) < 1:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_MISSING_LEG: buckets[{index}] must contain legs[0]."
            )
        raw_leg = bucket_legs[0]
        if not isinstance(raw_leg, dict):
            raise ModelBlueValidationError(
                f"MODEL_BLUE_INVALID_LEG: buckets[{index}].legs[0] must be an object."
            )

        symbol = str(raw_leg.get("underlying") or bucket.get("underlying") or "").strip()
        if not symbol:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_MISSING_SYMBOL: buckets[{index}] has no underlying/symbol."
            )

        instrument_type = str(raw_leg.get("instrument_type") or "").strip()
        if not instrument_type:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_MISSING_INSTRUMENT_TYPE: buckets[{index}] missing instrument_type."
            )

        try:
            weight = float(raw_leg["weight"])
        except (KeyError, TypeError, ValueError):
            raise ModelBlueValidationError(
                f"MODEL_BLUE_MISSING_WEIGHT: buckets[{index}] missing numeric weight."
            ) from None
        if weight == 0:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_INVALID_WEIGHT: buckets[{index}] weight must be non-zero."
            )

        price = _parse_price(raw_leg.get("price"), index)
        payload_side = raw_leg.get("side")
        parsed.append(
            SignalLeg(
                symbol=symbol,
                instrument_type=instrument_type,
                weight=weight,
                price=price,
                payload_side=str(payload_side).upper() if payload_side is not None else None,
                leg_index=index,
            )
        )

    return tuple(parsed)


def _parse_price(raw: Any, index: int) -> Decimal:
    if raw is None:
        raise ModelBlueValidationError(
            f"MODEL_BLUE_MISSING_PRICE: buckets[{index}] missing price."
        )
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ModelBlueValidationError(
            f"MODEL_BLUE_INVALID_PRICE: buckets[{index}] price {raw!r} is not numeric."
        ) from exc
    if price <= 0:
        raise ModelBlueValidationError(
            f"MODEL_BLUE_INVALID_PRICE: buckets[{index}] price must be positive."
        )
    return price
