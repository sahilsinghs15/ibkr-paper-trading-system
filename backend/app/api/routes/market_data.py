"""Market data endpoint router."""

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_market_data_adapter, get_trading_service
from app.models.market_data import MarketDataEvent
from app.schemas.api_schemas import (
    MarketDataEventRequest,
    MarketDataResponse,
    MarketDataSubscriptionResponse,
    OrderSchema,
    SignalSchema,
)
from app.services.trading_service import TradingService

if TYPE_CHECKING:
    from app.market_data.ibkr_market_data import IBKRMarketDataAdapter

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


@router.post(
    "/market-data/subscribe",
    response_model=MarketDataSubscriptionResponse,
    summary="Subscribe to TWS market data",
)
async def subscribe_market_data(
    adapter: "IBKRMarketDataAdapter | None" = Depends(get_market_data_adapter),
) -> MarketDataSubscriptionResponse:
    """Start TWS market data subscription for the configured symbol.
    
    Only available in IBKR mode. The market data will automatically stream
    to the strategy pipeline via the background consumer task.
    """
    if adapter is None:
        raise HTTPException(
            status_code=400,
            detail="Market data subscription is only available in IBKR mode.",
        )
        
    try:
        req_id = adapter.request_market_data()
        return MarketDataSubscriptionResponse(
            subscribed=True,
            symbol=adapter._settings.ibkr_market_data_symbol,
            request_id=req_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/market-data/subscribe",
    response_model=MarketDataSubscriptionResponse,
    summary="Cancel TWS market data subscription",
)
async def cancel_market_data_subscription(
    adapter: "IBKRMarketDataAdapter | None" = Depends(get_market_data_adapter),
) -> MarketDataSubscriptionResponse:
    """Cancel an active TWS market data subscription."""
    if adapter is None:
        raise HTTPException(
            status_code=400,
            detail="Market data subscription is only available in IBKR mode.",
        )
        
    adapter.cancel_market_data()
    return MarketDataSubscriptionResponse(
        subscribed=False,
        symbol=None,
        request_id=None,
    )
