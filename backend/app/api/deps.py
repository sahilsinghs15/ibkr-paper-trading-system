"""Dependency injection helpers for API endpoints."""

from fastapi import Request

from app.oms.oms_service import OMSService
from app.services.order_manager import OrderManager


def get_oms(request: Request) -> OMSService:
    """Retrieve the global OMSService instance from application state."""
    return request.app.state.oms


def get_order_manager(request: Request) -> OrderManager:
    """Retrieve the global OrderManager instance from application state."""
    return request.app.state.order_manager
