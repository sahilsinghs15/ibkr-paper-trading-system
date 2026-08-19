"""Webhook inbound parse: identify strategy, then delegate."""

import logging
from datetime import datetime
from typing import Any

from app.models.signal import Signal
from app.services.model_blue.strategy import ModelBlueStrategy
from app.services.strategies.legacy import parse_legacy_signal
from app.services.strategies.registry import StrategyRegistry

logger = logging.getLogger(__name__)

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
        logger.info(
            "Inbound parse: handler=%s strategy_id=%s action=%s trade_id=%s request_id=%s",
            type(handler).__name__,
            strategy_id,
            payload.get("action"),
            payload.get("trade_id"),
            request_id,
        )
        return handler.parse_payload(
            payload,
            timestamp=timestamp,
            reason=f"TradingView webhook request_id={request_id}",
            raw_payload=capture_data,
        )
    logger.info(
        "Inbound parse: handler=legacy strategy_id=%s action=%s trade_id=%s request_id=%s",
        strategy_id,
        payload.get("action"),
        payload.get("trade_id"),
        request_id,
    )
    return parse_legacy_signal(
        payload,
        strategy_id=strategy_id,
        request_id=request_id,
        utc_now=timestamp,
        capture_data=capture_data,
    )
