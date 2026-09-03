"""Live IBKR account-margin read endpoints."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import require_admin, require_authenticated_user
from app.api.routes.config import _check_account_authorization
from app.db.models.user import UserModel
from app.rms.margin_estimate import (
    effective_free_margin,
    headroom_floor,
    pending_commitments,
)
from app.rms.models import MarginPolicy
from app.schemas.margin_schemas import AccountMarginListResponse, AccountMarginResponse
from app.services.account_margin import AccountMarginService, AccountMarginSnapshot

router = APIRouter(prefix="/margin", tags=["margin"])


def _service(request: Request) -> AccountMarginService:
    svc = getattr(request.app.state, "account_margin", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Account margin service unavailable.")
    client = getattr(request.app.state, "client", None)
    if client is not None and hasattr(client, "is_connected") and not client.is_connected():
        raise HTTPException(status_code=503, detail="TWS gateway is down.")
    return svc


def _policy(request: Request) -> MarginPolicy:
    om = getattr(request.app.state, "order_manager", None)
    if om is None:
        return MarginPolicy()
    return getattr(om._rms_context, "margin_policy", MarginPolicy())


def _commitments(request: Request, account: str) -> list:
    om = getattr(request.app.state, "order_manager", None)
    if om is None:
        return []
    return om._rms_context.margin_commitments.get(account, [])


def _to_response(
    snapshot: AccountMarginSnapshot, policy: MarginPolicy, commitments: list
) -> AccountMarginResponse:
    free = snapshot.free_margin(policy.gate_basis)
    pending = pending_commitments(commitments, snapshot.as_of)
    effective = effective_free_margin(snapshot, commitments, policy)
    floor = headroom_floor(snapshot, policy)
    utilisation = None
    if snapshot.net_liquidation and snapshot.net_liquidation > 0 and effective is not None:
        used = snapshot.net_liquidation - effective
        utilisation = (used / snapshot.net_liquidation) * Decimal(100)
    return AccountMarginResponse(
        ibkr_account=snapshot.ibkr_account,
        currency=snapshot.currency,
        as_of=snapshot.as_of,
        is_stale=snapshot.is_stale,
        gate_basis=policy.gate_basis,
        net_liquidation=snapshot.net_liquidation,
        available_funds=snapshot.available_funds,
        excess_liquidity=snapshot.excess_liquidity,
        full_init_margin_req=snapshot.full_init_margin_req,
        full_maint_margin_req=snapshot.full_maint_margin_req,
        buying_power=snapshot.buying_power,
        gross_position_value=snapshot.gross_position_value,
        total_cash_value=snapshot.total_cash_value,
        cushion=snapshot.cushion,
        look_ahead_init_margin_req=snapshot.look_ahead_init_margin_req,
        look_ahead_maint_margin_req=snapshot.look_ahead_maint_margin_req,
        look_ahead_available_funds=snapshot.look_ahead_available_funds,
        look_ahead_excess_liquidity=snapshot.look_ahead_excess_liquidity,
        look_ahead_next_change=snapshot.look_ahead_next_change,
        free_margin=free,
        effective_free_margin=effective,
        pending_commitments=pending,
        floor=floor,
        utilisation_pct=utilisation,
    )


@router.get(
    "/accounts",
    response_model=AccountMarginListResponse,
    summary="List live margin snapshots for all managed accounts",
)
async def list_account_margins(
    request: Request,
    _admin: UserModel = Depends(require_admin),
) -> AccountMarginListResponse:
    svc = _service(request)
    policy = _policy(request)
    snapshots = svc.all_snapshots()
    if not snapshots:
        client = getattr(request.app.state, "client", None)
        if client is None or not client.is_connected():
            raise HTTPException(status_code=503, detail="TWS gateway is down.")
    accounts = [
        _to_response(snap, policy, _commitments(request, key))
        for key, snap in sorted(snapshots.items())
    ]
    return AccountMarginListResponse(accounts=accounts)


@router.get(
    "/accounts/{ibkr_account}",
    response_model=AccountMarginResponse,
    summary="Live margin snapshot for one IBKR account",
)
async def get_account_margin(
    ibkr_account: str,
    request: Request,
    current_user: UserModel = Depends(require_authenticated_user),
) -> AccountMarginResponse:
    _check_account_authorization(current_user, ibkr_account=ibkr_account)
    svc = _service(request)
    snap = svc.snapshot_for(ibkr_account)
    if snap is None:
        raise HTTPException(
            status_code=503,
            detail=f"No live margin snapshot for {ibkr_account.strip().upper()}.",
        )
    return _to_response(
        snap, _policy(request), _commitments(request, ibkr_account.strip().upper())
    )
