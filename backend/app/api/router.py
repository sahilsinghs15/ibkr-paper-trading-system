"""Main API v1 router definition."""

from fastapi import APIRouter

from app.api.routes.auth import router as auth_router
from app.api.routes.baskets import router as baskets_router
from app.api.routes.config import router as config_router
from app.api.routes.emergency import router as emergency_router
from app.api.routes.orders import router as orders_router
from app.api.routes.reconcile import router as reconcile_router
from app.api.routes.system_monitor import router as system_monitor_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(orders_router)
api_router.include_router(baskets_router)
api_router.include_router(config_router)
api_router.include_router(emergency_router)
api_router.include_router(system_monitor_router)
api_router.include_router(reconcile_router)

