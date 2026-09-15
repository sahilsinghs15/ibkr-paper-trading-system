"""System Monitor read-only operational observability endpoint."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models.user import UserModel
from app.db.session import AsyncSessionLocal, get_db_session
from app.schemas.system_monitor import CreditUsageResponse, SystemMonitorResponse
from app.services.instance_credit.metadata import discover_instance_identity
from app.services.instance_credit.service import InstanceCreditService
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


@router.get(
    "/credit",
    summary="Get instance credit usage ledger",
    description="Returns This Month Credit Usage and Daily Instance Cost derived from persisted daily ledger (no AWS call).",
    response_model=CreditUsageResponse,
)
async def get_credit_usage(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[UserModel, Depends(require_admin)],
) -> CreditUsageResponse:
    """Retrieve credit usage without triggering AWS Cost Explorer."""
    svc = InstanceCreditService(AsyncSessionLocal)
    # Reuse monitor helper for consistency: leverage same calculation path
    from app.services.system_monitor_service import _collect_credit_usage

    credit = await _collect_credit_usage(db)
    if credit is None:
        raise HTTPException(status_code=503, detail="Credit ledger unavailable")
    return credit


@router.post(
    "/credit/refresh",
    summary="Admin refresh: fetch previous day actual from AWS",
    description="Manually trigger AWS Cost Explorer fetch for previous UTC day. Admin only, at most once per day IS NOT enforced here but underlying ledger is idempotent.",
    response_model=dict,
)
async def refresh_credit_usage(
    _admin: Annotated[UserModel, Depends(require_admin)],
) -> dict:
    """Admin-triggered AWS fetch (outside normal daily schedule)."""
    identity = await discover_instance_identity()
    if identity is None:
        raise HTTPException(status_code=503, detail="Instance identity unavailable (no IMDS / env config)")
    svc = InstanceCreditService(AsyncSessionLocal)
    target_date = (datetime.now(UTC) - timedelta(days=1)).date()
    result = await svc.fetch_and_persist_for_date(target_date, identity=identity)
    if result is None:
        # Could be missing data or error; report to caller
        return {"status": "no_data", "target_date": target_date.isoformat(), "detail": "No cost data available or fetch failed (see logs, ledger not overwritten with $0)"}
    return {
        "status": "ok",
        "target_date": target_date.isoformat(),
        "total_cost_usd": str(result.total_cost_usd) if result.total_cost_usd is not None else None,
        "ec2_cost_usd": str(result.ec2_cost_usd) if result.ec2_cost_usd is not None else None,
        "public_ipv4_cost_usd": str(result.public_ipv4_cost_usd) if result.public_ipv4_cost_usd is not None else None,
        "source": result.source,
    }
