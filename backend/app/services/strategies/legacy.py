"""Legacy single-name TradingView payload parsing (non-strategy-module path)."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.signal import Signal, SignalType


def _extract_decimal(val: Any, default: Decimal = Decimal(0)) -> Decimal:
    if val is None:
        return default
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return default


def parse_legacy_signal(
    payload: dict[str, Any],
    *,
    strategy_id: str,
    request_id: str,
    utc_now: datetime,
    capture_data: dict[str, Any],
) -> Signal:
    raw_signal_id = payload.get("signal_id") or payload.get("trade_id") or request_id
    signal_id = str(raw_signal_id)

    action = str(payload.get("action") or payload.get("signal_type") or "OPEN").upper()
    pair = str(
        payload.get("pair") or payload.get("ticker") or payload.get("symbol") or "N/A"
    )
    side = str(payload.get("side") or payload.get("direction") or "BUY").upper()

    ref_price_a = _extract_decimal(payload.get("ref_price_a", payload.get("price")))

    raw_qty = payload.get("quantity") or payload.get("qty") or payload.get("position_size")
    quantity: int | None = None
    if raw_qty is not None:
        try:
            quantity = int(float(str(raw_qty)))
        except (ValueError, TypeError):
            quantity = None

    sig_type_str = str(payload.get("action") or payload.get("signal_type") or "BUY").upper()
    if sig_type_str in ("HOLD",):
        sig_type = SignalType.HOLD
    elif sig_type_str in ("SELL", "SHORT", "CLOSE"):
        sig_type = SignalType.SELL
    else:
        sig_type = SignalType.BUY

    return Signal(
        signal_type=sig_type,
        timestamp=utc_now,
        reason=f"TradingView webhook request_id={request_id}",
        signal_id=signal_id,
        strategy_id=strategy_id,
        action=action,
        symbol=pair if pair != "N/A" else None,
        side=side,
        price=ref_price_a if ref_price_a > 0 else None,
        quantity=quantity,
        raw_payload=capture_data,
    )
