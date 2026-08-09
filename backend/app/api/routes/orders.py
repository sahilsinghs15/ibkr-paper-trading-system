"""Orders endpoint router."""

from fastapi import APIRouter, Depends

from app.api.deps import get_broker
from app.broker.base_broker import BaseBroker
from app.schemas.api_schemas import OrderSchema

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
