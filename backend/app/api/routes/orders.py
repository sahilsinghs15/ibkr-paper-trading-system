"""Orders endpoint router querying OMSService."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_oms, require_authenticated_user
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
        orders = [o for o in orders if o.account == user_account]
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
        if order.account != user_account:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found.")
    return OrderSchema.model_validate(order)


@router.delete(
    "/orders/{order_id}",
    response_model=OrderSchema,
    summary="Cancel an active order",
)
async def cancel_order(
    order_id: str,
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
        if order.account != user_account:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found.")
    try:
        canceled_order = await oms.cancel_order(order_id)
        logger.info(
            "HTTP cancel order result: order_id=%s status=%s",
            order_id,
            canceled_order.status.value,
        )
        return OrderSchema.model_validate(canceled_order)
    except ValueError as e:
        logger.warning("HTTP cancel order failed: order_id=%s error=%s", order_id, e)
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
