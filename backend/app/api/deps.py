"""Dependency injection helpers for API endpoints."""

from typing import TYPE_CHECKING

from fastapi import Request

from app.broker.base_broker import BaseBroker
from app.services.trading_service import TradingService

if TYPE_CHECKING:
    from app.market_data.ibkr_market_data import IBKRMarketDataAdapter


def get_trading_service(request: Request) -> TradingService:
    """Retrieve the global TradingService instance from application state."""
    return request.app.state.trading_service


def get_broker(request: Request) -> BaseBroker:
    """Retrieve the global BaseBroker instance from application state."""
    return request.app.state.broker


def get_market_data_adapter(request: Request) -> "IBKRMarketDataAdapter | None":
    """Retrieve the IBKRMarketDataAdapter if running in IBKR mode, else None."""
    return getattr(request.app.state, "market_data_adapter", None)
