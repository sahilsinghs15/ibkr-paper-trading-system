"""Dependency injection helpers for API endpoints."""

from fastapi import Request

from app.broker.base_broker import BaseBroker
from app.services.trading_service import TradingService


def get_trading_service(request: Request) -> TradingService:
    """Retrieve the global TradingService instance from application state."""
    return request.app.state.trading_service


def get_broker(request: Request) -> BaseBroker:
    """Retrieve the global BaseBroker instance from application state."""
    return request.app.state.broker
