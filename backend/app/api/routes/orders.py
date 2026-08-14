"""Orders endpoint router querying OMSService."""

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_oms
from app.oms.oms_service import OMSService
from app.schemas.api_schemas import OrderSchema

router = APIRouter(tags=["orders"])


@router.get(
    "/orders",
    response_model=list[OrderSchema],
    summary="Get all active orders",
)
async def get_orders(
    oms: OMSService = Depends(get_oms),
) -> list[OrderSchema]:
    """Retrieve all tracked internal orders from the OMS."""
    orders = oms.get_all_orders()
    return [OrderSchema.model_validate(o) for o in orders]


@router.get(
    "/orders/{order_id}",
    response_model=OrderSchema,
    summary="Get order by ID",
)
async def get_order_by_id(
    order_id: str,
    oms: OMSService = Depends(get_oms),
) -> OrderSchema:
    """Retrieve a specific order by its internal order ID."""
    order = oms.get_order(order_id)
    if order is None:
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
) -> OrderSchema:
    """Submit a cancel request for an open order through the OMS."""
    try:
        order = await oms.cancel_order(order_id)
        return OrderSchema.model_validate(order)
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
