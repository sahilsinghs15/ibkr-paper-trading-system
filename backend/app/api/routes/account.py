"""Account and portfolio endpoint router."""

from fastapi import APIRouter, Depends

from app.api.deps import get_broker
from app.broker.base_broker import BaseBroker
from app.schemas.api_schemas import MarginSchema, PositionSchema

router = APIRouter(tags=["account"])


@router.get(
    "/positions",
    response_model=list[PositionSchema],
    summary="Get current positions",
)
async def get_positions(
    broker: BaseBroker = Depends(get_broker),
) -> list[PositionSchema]:
    """Retrieve all non-flat portfolio positions from the broker."""
    positions = await broker.get_positions()
    # Pydantic handles coercion from domain dataclasses to API schemas
    return [PositionSchema.model_validate(p) for p in positions]


@router.get(
    "/margin",
    response_model=MarginSchema,
    summary="Get account margin info",
)
async def get_margin(broker: BaseBroker = Depends(get_broker)) -> MarginSchema:
    """Retrieve margin, equity, and buying power details from the broker."""
    margin = await broker.get_margin()
    return MarginSchema.model_validate(margin)
