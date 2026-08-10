"""Orders endpoint router."""

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_broker
from app.broker.base_broker import BaseBroker
from app.schemas.api_schemas import (
    ModifyOrderRequest,
    OrderSchema,
    PlaceOrderRequest,
)

router = APIRouter(tags=["orders"])


@router.get(
    "/orders",
    response_model=list[OrderSchema],
    summary="Get all orders",
)
async def get_orders(
    broker: BaseBroker = Depends(get_broker),
) -> list[OrderSchema]:
    """Retrieve the current list of orders (order book) from the broker."""
    orders = await broker.get_order_book()
    return [OrderSchema.model_validate(o) for o in orders]


@router.post(
    "/orders",
    response_model=OrderSchema,
    summary="Place a new order",
)
async def place_order(
    request: PlaceOrderRequest,
    broker: BaseBroker = Depends(get_broker),
) -> OrderSchema:
    """Place a new order via the active broker."""
    try:
        order = await broker.place_order(
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            price=request.price,
        )
        return OrderSchema.model_validate(order)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put(
    "/orders/{order_id}",
    response_model=OrderSchema,
    summary="Modify an existing order",
)
async def modify_order(
    order_id: str,
    request: ModifyOrderRequest,
    broker: BaseBroker = Depends(get_broker),
) -> OrderSchema:
    """Modify an open order's quantity or price."""
    try:
        order = await broker.modify_order(
            order_id=order_id,
            quantity=request.quantity,
            price=request.price,
        )
        return OrderSchema.model_validate(order)
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/orders/{order_id}",
    response_model=OrderSchema,
    summary="Cancel an existing order",
)
async def cancel_order(
    order_id: str,
    broker: BaseBroker = Depends(get_broker),
) -> OrderSchema:
    """Submit a cancel request for an open order."""
    try:
        order = await broker.cancel_order(order_id=order_id)
        return OrderSchema.model_validate(order)
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
