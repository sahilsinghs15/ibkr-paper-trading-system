"""Market data endpoint router."""

from fastapi import APIRouter, Depends

from app.api.deps import get_trading_service
from app.models.market_data import MarketDataEvent
from app.schemas.api_schemas import (
    MarketDataEventRequest,
    MarketDataResponse,
    OrderSchema,
    SignalSchema,
)
from app.services.trading_service import TradingService

router = APIRouter(tags=["market-data"])


@router.post(
    "/market-data",
    response_model=MarketDataResponse,
    summary="Submit market data tick",
)
async def post_market_data(
    payload: MarketDataEventRequest,
    service: TradingService = Depends(get_trading_service),
) -> MarketDataResponse:
    """Ingest a market-data price update and orchestrate strategy evaluation

    and order execution if a candle completes.
    """
    # Instantiate the domain model (triggers timezone validation)
    event = MarketDataEvent(
        timestamp=payload.timestamp,
        price=payload.price,
        volume=payload.volume,
    )

    result = await service.process_market_data(event)

    if result is None:
        return MarketDataResponse(
            candle_completed=False,
            signal=None,
            order=None,
        )

    signal, order = result
    return MarketDataResponse(
        candle_completed=True,
        signal=SignalSchema.model_validate(signal),
        order=OrderSchema.model_validate(order) if order is not None else None,
    )
