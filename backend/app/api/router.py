"""Main API v1 router definition."""

from fastapi import APIRouter

from app.api.routes.account import router as account_router
from app.api.routes.broker import router as broker_router
from app.api.routes.market_data import router as market_data_router
from app.api.routes.orders import router as orders_router

api_router = APIRouter()

api_router.include_router(market_data_router)
api_router.include_router(orders_router)
api_router.include_router(account_router)
api_router.include_router(broker_router)
