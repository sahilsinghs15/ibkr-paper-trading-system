"""Main API v1 router definition."""

from fastapi import APIRouter

from app.api.routes.config import router as config_router
from app.api.routes.emergency import router as emergency_router
from app.api.routes.orders import router as orders_router

api_router = APIRouter()

api_router.include_router(orders_router)
api_router.include_router(config_router)
api_router.include_router(emergency_router)
