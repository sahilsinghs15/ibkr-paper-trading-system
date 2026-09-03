"""System Monitor read-only operational observability endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models.user import UserModel
from app.db.session import get_db_session
from app.schemas.system_monitor import SystemMonitorResponse
from app.services.system_monitor_service import collect_system_monitor_data

router = APIRouter(prefix="/system-monitor", tags=["system-monitor"])


@router.get(
    "",
    summary="Get operational system monitor metrics",
    description="Read-only observability endpoint returning EC2 system resource metrics, storage utilization, service health states, top processes, and operational alerts.",
    response_model=SystemMonitorResponse,
)
async def get_system_monitor(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[UserModel, Depends(require_admin)],
) -> SystemMonitorResponse:
    """Retrieve structured system resource and service health observability data."""
    tws_client = getattr(request.app.state, "client", None) or getattr(
        request.app.state, "tws_client", None
    )
    redis_client = getattr(request.app.state, "redis_client", None)
    account_margin = getattr(request.app.state, "account_margin", None)
    return await collect_system_monitor_data(
        session=db,
        tws_client=tws_client,
        redis_client=redis_client,
        account_margin=account_margin,
    )
