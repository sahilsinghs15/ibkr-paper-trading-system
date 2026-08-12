"""TradingView Webhook router definition."""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.schemas.webhook import TradingViewWebhookResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/tradingview",
    response_model=TradingViewWebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive TradingView Webhook",
    description="Minimal connectivity POC endpoint to receive and log TradingView alert JSON payloads.",
)
async def receive_tradingview_webhook(request: Request) -> dict[str, str]:
    """Ingest, validate, and safely log a TradingView JSON alert payload."""
    try:
        payload: Any = await request.json()
    except Exception as e:
        logger.warning(
            "Malformed or unparseable JSON payload received on TradingView webhook: %s",
            e,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or malformed JSON payload.",
        ) from e

    if not isinstance(payload, dict):
        logger.warning(
            "Non-dictionary JSON payload received on TradingView webhook: type=%s",
            type(payload).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JSON payload must be a dictionary object.",
        )

    logger.info("Received TradingView webhook payload safely: %s", payload)

    return {"status": "received", "source": "tradingview"}
