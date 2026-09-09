"""Read-only reconcile positions dashboard endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_authenticated_user
from app.core.identifiers import normalize_account
from app.db.models.user import UserModel
from app.db.session import get_db_session
from app.schemas.reconcile_schemas import (
    AlignBrokerPositionRequest,
    AlignBrokerPositionResponse,
    FlattenBrokerPositionRequest,
    FlattenBrokerPositionResponse,
    ReconcilePositionsResponse,
)
from app.services.broker_align_service import BrokerAlignService
from app.services.broker_flatten_service import BrokerFlattenService
from app.services.order_manager import OrderManager
from app.services.reconcile_service import collect_reconcile_positions

router = APIRouter(prefix="/reconcile", tags=["reconcile"])


@router.get(
    "/positions",
    summary="Get broker snapshot, ledger rows, and reconcile diffs",
    description=(
        "View of the latest persisted IBKR broker snapshot, OPEN Model Blue ledger pair "
        "rows, and freshly classified broker-vs-ledger diffs. By default data reflects the "
        "background reconciler's last sweep. Pass refresh=true to run one live reqPositions "
        "sweep first (skipped when TWS is disconnected or a sweep is already running)."
    ),
    response_model=ReconcilePositionsResponse,
)
async def get_reconcile_positions(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[
        str | None,
        Query(description="Optional IBKR account filter (e.g. DUR919062)"),
    ] = None,
    refresh: Annotated[
        bool,
        Query(
            description=(
                "When true, run one IBKR position reconcile sweep before returning data."
            ),
        ),
    ] = False,
) -> ReconcilePositionsResponse:
    """Return reconcile dashboard payload for one account or all accounts."""
    if current_user.role == "user":
        user_account = current_user.account.ibkr_account if current_user.account else None
        if ibkr_account and normalize_account(ibkr_account) != normalize_account(user_account):
            raise HTTPException(status_code=403, detail="Forbidden: Cannot access another account")
        ibkr_account = user_account
    if refresh:
        reconciler = getattr(request.app.state, "position_reconciler", None)
        if reconciler is not None:
            await reconciler.run_once()
    return await collect_reconcile_positions(db, ibkr_account=ibkr_account)


@router.post(
    "/positions/flatten",
    summary="Flatten one IBKR broker snapshot line",
    description=(
        "Submit a MARKET reverse for the persisted broker_positions line identified by "
        "ibkr_account and con_id. Quantity comes from the request and is capped by the "
        "snapshot; side comes from the snapshot sign. Does not arm the kill switch or "
        "mutate the Model Blue positions ledger."
    ),
    response_model=FlattenBrokerPositionResponse,
)
async def flatten_broker_position_line(
    body: FlattenBrokerPositionRequest,
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
) -> FlattenBrokerPositionResponse:
    """Flatten one broker snapshot net line."""
    if current_user.role == "user":
        user_account = current_user.account.ibkr_account if current_user.account else None
        if normalize_account(body.ibkr_account) != normalize_account(user_account):
            raise HTTPException(status_code=403, detail="Forbidden: Cannot access another account")

    order_manager: OrderManager | None = getattr(request.app.state, "order_manager", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=503,
            detail="Session factory is unavailable.",
        )

    svc = BrokerFlattenService(session_factory=session_factory, order_manager=order_manager)
    return await svc.flatten_line(
        ibkr_account=body.ibkr_account.strip(),
        symbol=body.symbol.strip(),
        sec_type=body.sec_type.strip(),
        con_id=body.con_id,
        quantity=body.quantity,
    )


@router.post(
    "/positions/align",
    summary="Align one IBKR broker net line to the OPEN ledger",
    description=(
        "Submit a MARKET trade so the persisted broker snapshot net matches the OPEN "
        "Model Blue ledger net for the symbol. Side and quantity are computed server-side "
        "from broker vs ledger delta. Does not arm the kill switch or mutate ledger pairs."
    ),
    response_model=AlignBrokerPositionResponse,
)
async def align_broker_position_line(
    body: AlignBrokerPositionRequest,
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
) -> AlignBrokerPositionResponse:
    """Align one broker net line to the signal ledger."""
    if current_user.role == "user":
        user_account = current_user.account.ibkr_account if current_user.account else None
        if normalize_account(body.ibkr_account) != normalize_account(user_account):
            raise HTTPException(status_code=403, detail="Forbidden: Cannot access another account")

    order_manager: OrderManager | None = getattr(request.app.state, "order_manager", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=503,
            detail="Session factory is unavailable.",
        )

    svc = BrokerAlignService(session_factory=session_factory, order_manager=order_manager)
    return await svc.align_line(
        ibkr_account=body.ibkr_account.strip(),
        symbol=body.symbol.strip(),
        sec_type=body.sec_type.strip(),
        con_id=body.con_id,
    )
