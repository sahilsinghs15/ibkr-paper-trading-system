"""Orders endpoint router querying OMSService."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_oms, require_authenticated_user
from app.audit.context import actor_for_user
from app.audit.recorder import audit_entry, get_audit_recorder
from app.audit.taxonomy import AuditAction, AuditResult
from app.db.models.user import UserModel
from app.oms.oms_service import OMSService
from app.schemas.api_schemas import OrderSchema

logger = logging.getLogger(__name__)

router = APIRouter(tags=["orders"])


@router.get(
    "/orders",
    response_model=list[OrderSchema],
    summary="Get all active orders",
)
async def get_orders(
    oms: OMSService = Depends(get_oms),
    current_user: UserModel = Depends(require_authenticated_user),
) -> list[OrderSchema]:
    """Retrieve all tracked internal orders from the OMS authorized for caller."""
    orders = oms.get_all_orders()
    if current_user.role == "user":
        user_account = current_user.account.ibkr_account if current_user.account else None
        orders = [
            o for o in orders if getattr(o.intent, "ibkr_account", None) == user_account
        ]
    return [OrderSchema.model_validate(o) for o in orders]


@router.get(
    "/orders/{order_id}",
    response_model=OrderSchema,
    summary="Get order by ID",
)
async def get_order_by_id(
    order_id: str,
    oms: OMSService = Depends(get_oms),
    current_user: UserModel = Depends(require_authenticated_user),
) -> OrderSchema:
    """Retrieve a specific order by its internal order ID."""
    order = oms.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found.")
    if current_user.role == "user":
        user_account = current_user.account.ibkr_account if current_user.account else None
        if getattr(order.intent, "ibkr_account", None) != user_account:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found.")
    return OrderSchema.model_validate(order)


@router.delete(
    "/orders/{order_id}",
    response_model=OrderSchema,
    summary="Cancel an active order",
)
async def cancel_order(
    order_id: str,
    request: Request,
    oms: OMSService = Depends(get_oms),
    current_user: UserModel = Depends(require_authenticated_user),
) -> OrderSchema:
    """Submit a cancel request for an open order through the OMS."""
    logger.info("HTTP cancel order request: order_id=%s", order_id)
    order = oms.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found.")
    if current_user.role == "user":
        user_account = current_user.account.ibkr_account if current_user.account else None
        if getattr(order.intent, "ibkr_account", None) != user_account:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found.")
    before = OrderSchema.model_validate(order)
    entry = audit_entry(
        request,
        action=AuditAction.ENGINE_ORDER_CANCEL,
        actor=actor_for_user(request, current_user),
        summary=f"Cancel engine order {order_id} ({order.symbol})",
        account_id=getattr(order.intent, "account_id", None),
        ibkr_account=getattr(order.intent, "ibkr_account", None),
        target_type="ENGINE_ORDER",
        target_id=order_id,
        parameters={"order_id": order_id},
        before_state=before,
        related={"internal_order_id": order_id},
    )
    async with get_audit_recorder(request).operation(entry) as audit_op:
        try:
            canceled_order = await oms.cancel_order(order_id)
            logger.info(
                "HTTP cancel order result: order_id=%s status=%s",
                order_id,
                canceled_order.status.value,
            )
            result = OrderSchema.model_validate(canceled_order)
            audit_op.set_outcome(AuditResult.SUCCEEDED, after=result)
            return result
        except ValueError as e:
            logger.warning("HTTP cancel order failed: order_id=%s error=%s", order_id, e)
            if "not found" in str(e).lower():
                raise HTTPException(status_code=404, detail=str(e))
            raise HTTPException(status_code=400, detail=str(e))
