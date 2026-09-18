"""Reconcile (inventory) dashboard endpoint and operator inventory fixes."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_authenticated_user
from app.audit.context import actor_for_user
from app.audit.recorder import audit_entry, get_audit_recorder
from app.audit.taxonomy import (
    ACTION_CATALOG,
    INVENTORY_FIX_ACTIONS,
    AuditAction,
    AuditResult,
)
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

logger = logging.getLogger(__name__)

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


async def _inventory_snapshot(
    db: AsyncSession, *, ibkr_account: str, con_id: int, symbol: str, sec_type: str
) -> dict[str, Any] | None:
    """Server-side broker-vs-ledger state for one line, as classified right now.

    This is the authoritative "before" state for an inventory fix and the source
    of the fix type (never taken from the client).
    """
    try:
        payload = await collect_reconcile_positions(db, ibkr_account=ibkr_account)
    except Exception:
        logger.exception("Inventory audit snapshot failed ibkr_account=%s", ibkr_account)
        return None
    sym = symbol.strip().upper()
    stype = sec_type.strip().upper()
    diff = next(
        (d for d in payload.diffs if d.con_id == con_id),
        None,
    ) or next(
        (d for d in payload.diffs if d.symbol.upper() == sym and d.sec_type.upper() == stype),
        None,
    )
    broker_line = next((b for b in payload.broker_positions if b.con_id == con_id), None)
    return {
        "diff": diff,
        "broker_line": broker_line,
        "reconcile_run": payload.run,
    }


def _inventory_entry(
    request: Request,
    current_user: UserModel,
    *,
    action: AuditAction,
    summary: str,
    body: FlattenBrokerPositionRequest | AlignBrokerPositionRequest,
    snapshot: dict[str, Any] | None,
    fix_type: str,
):
    diff = snapshot.get("diff") if snapshot else None
    account_id = getattr(diff, "account_id", None)
    return audit_entry(
        request,
        action=action,
        actor=actor_for_user(request, current_user),
        summary=summary,
        account_id=account_id,
        ibkr_account=normalize_account(body.ibkr_account),
        target_type="BROKER_POSITION_LINE",
        target_id=f"{body.symbol.strip().upper()} con_id={body.con_id}",
        parameters={**body.model_dump(), "fix_type": fix_type},
        before_state=snapshot,
        related={"con_id": body.con_id, "symbol": body.symbol.strip().upper()},
    )


def _inventory_result(response: FlattenBrokerPositionResponse) -> AuditResult:
    if response.success:
        return AuditResult.SUCCEEDED
    if response.status == "PARTIAL":
        return AuditResult.PARTIAL
    return AuditResult.FAILED


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
    db: Annotated[AsyncSession, Depends(get_db_session)],
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

    snapshot = await _inventory_snapshot(
        db,
        ibkr_account=body.ibkr_account.strip(),
        con_id=body.con_id,
        symbol=body.symbol,
        sec_type=body.sec_type,
    )
    entry = _inventory_entry(
        request,
        current_user,
        action=AuditAction.INVENTORY_BROKER_LINE_FLATTEN,
        summary=f"Flatten broker line {body.symbol.strip().upper()} qty {body.quantity}",
        body=body,
        snapshot=snapshot,
        fix_type="BROKER_LINE_FLATTEN",
    )
    svc = BrokerFlattenService(session_factory=session_factory, order_manager=order_manager)
    async with get_audit_recorder(request).operation(entry) as audit_op:
        response = await svc.flatten_line(
            ibkr_account=body.ibkr_account.strip(),
            symbol=body.symbol.strip(),
            sec_type=body.sec_type.strip(),
            con_id=body.con_id,
            quantity=body.quantity,
        )
        audit_op.set_outcome(
            _inventory_result(response),
            reason=None if response.success else response.message,
            after=response,
        )
    return response


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
    db: Annotated[AsyncSession, Depends(get_db_session)],
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

    snapshot = await _inventory_snapshot(
        db,
        ibkr_account=body.ibkr_account.strip(),
        con_id=body.con_id,
        symbol=body.symbol,
        sec_type=body.sec_type,
    )
    diff = snapshot.get("diff") if snapshot else None
    diff_kind = getattr(diff, "kind", None)
    action = INVENTORY_FIX_ACTIONS.get(diff_kind or "", AuditAction.INVENTORY_FIX_UNCLASSIFIED)
    fix_label = ACTION_CATALOG[action][1]
    broker_qty = getattr(diff, "broker_qty", None)
    ledger_qty = getattr(diff, "ledger_qty", None)
    entry = _inventory_entry(
        request,
        current_user,
        action=action,
        summary=(
            f"{fix_label} — {body.symbol.strip().upper()} "
            f"(broker {broker_qty if broker_qty is not None else 'n/a'} vs "
            f"ledger {ledger_qty if ledger_qty is not None else 'n/a'})"
        ),
        body=body,
        snapshot=snapshot,
        fix_type=diff_kind or "UNCLASSIFIED",
    )
    svc = BrokerAlignService(session_factory=session_factory, order_manager=order_manager)
    async with get_audit_recorder(request).operation(entry) as audit_op:
        response = await svc.align_line(
            ibkr_account=body.ibkr_account.strip(),
            symbol=body.symbol.strip(),
            sec_type=body.sec_type.strip(),
            con_id=body.con_id,
        )
        expected_broker_qty = None
        if response.success and broker_qty is not None:
            signed = response.quantity if response.side == "BUY" else -response.quantity
            expected_broker_qty = broker_qty + signed
        audit_op.set_outcome(
            _inventory_result(response),
            reason=None if response.success else response.message,
            after={
                "execution": response,
                "expected_broker_qty_after_fill": expected_broker_qty,
                "ledger_qty": ledger_qty,
            },
        )
    return response
