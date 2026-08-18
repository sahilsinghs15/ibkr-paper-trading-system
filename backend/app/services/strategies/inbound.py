"""Webhook inbound parse: identify strategy, then delegate."""

from datetime import datetime
from typing import Any

from app.models.signal import Signal
from app.services.model_blue.strategy import ModelBlueStrategy
from app.services.strategies.legacy import parse_legacy_signal
from app.services.strategies.registry import StrategyRegistry

_PARSE_REGISTRY = StrategyRegistry([ModelBlueStrategy()])


def parse_tradingview_payload(
    payload: dict[str, Any],
    *,
    timestamp: datetime,
    request_id: str,
    capture_data: dict[str, Any],
    registry: StrategyRegistry | None = None,
) -> Signal:
    strategy_id = str(
        payload.get("strategy") or payload.get("strategy_id") or "default_strategy"
    )
    handler = (registry or _PARSE_REGISTRY).get(strategy_id)
    if handler is not None:
        return handler.parse_payload(
            payload,
            timestamp=timestamp,
            reason=f"TradingView webhook request_id={request_id}",
            raw_payload=capture_data,
        )
    return parse_legacy_signal(
        payload,
        strategy_id=strategy_id,
        request_id=request_id,
        utc_now=timestamp,
        capture_data=capture_data,
    )
