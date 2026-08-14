"""TradingView Webhook router definition."""

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.schemas.webhook import TradingViewWebhookResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

WEBHOOK_CAPTURE_DIR = (
    Path(__file__).resolve().parents[3] / "data" / "tradingview_webhooks"
)


@router.post(
    "/tradingview",
    response_model=TradingViewWebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive TradingView Webhook",
    description="Connectivity POC endpoint to receive, validate, log, and locally capture TradingView alert JSON payloads.",
)
async def receive_tradingview_webhook(request: Request) -> dict[str, str]:
    """Ingest, validate, safely log, and locally persist a TradingView JSON alert payload."""
    body_bytes = await request.body()
    raw_body = body_bytes.decode("utf-8", errors="replace")

    try:
        payload: Any = json.loads(body_bytes)
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

    request_id = str(uuid.uuid4())
    utc_now = datetime.now(UTC)
    received_at = utc_now.isoformat()

    capture_data = {
        "metadata": {
            "request_id": request_id,
            "received_at": received_at,
        },
        "raw_body": raw_body,
        "parsed_json": payload,
    }

    timestamp_str = utc_now.strftime("%Y%m%d_%H%M%S_%f")
    filename = f"webhook_{timestamp_str}_{request_id}.json"

    WEBHOOK_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    file_path = WEBHOOK_CAPTURE_DIR / filename
    file_path.write_text(json.dumps(capture_data, indent=2), encoding="utf-8")

    logger.info(
        "Received TradingView webhook payload safely (request_id=%s) and saved to %s: %s",
        request_id,
        file_path,
        payload,
    )

    return {"status": "received", "source": "tradingview"}

