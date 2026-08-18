"""TradingView Webhook router definition."""

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.schemas.webhook import TradingViewWebhookResponse
from app.services.strategies.inbound import parse_tradingview_payload

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
    description="Endpoint to receive, validate, log, locally capture, and persist TradingView alert JSON payloads into PostgreSQL.",
)
async def receive_tradingview_webhook(request: Request) -> dict[str, str]:
    """Ingest, validate, safely log, locally capture, and persist a TradingView JSON alert payload."""
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

    order_manager = getattr(request.app.state, "order_manager", None)
    try:
        if order_manager is not None:
            domain_signal = order_manager.parse_inbound_payload(
                payload,
                timestamp=utc_now,
                request_id=request_id,
                capture_data=capture_data,
            )
        else:
            domain_signal = parse_tradingview_payload(
                payload,
                timestamp=utc_now,
                request_id=request_id,
                capture_data=capture_data,
            )
    except ValueError as val_err:
        logger.warning("Rejected invalid TradingView payload: %s", val_err)
        return {"status": "rejected", "source": "tradingview"}

    if order_manager is not None:
        try:
            execution = await order_manager.process_signal_execution(domain_signal)
            if execution is not None:
                symbols = [o.symbol for o in execution.orders]
                logger.info(
                    "Signal processed by OrderManager -> RMS -> OMS: signal_id=%s orders=%s symbols=%s",
                    domain_signal.signal_id,
                    [o.internal_order_id for o in execution.orders],
                    symbols,
                )
        except ValueError as val_err:
            logger.warning("Incoming TradingView signal rejected: %s", val_err)
            return {"status": "rejected_by_rms", "source": "tradingview"}
        except (ConnectionError, RuntimeError) as conn_err:
            logger.warning("Signal ingested but broker submission unconfirmed: %s", conn_err)
            return {"status": "received", "source": "tradingview"}
        except Exception as exc:
            logger.exception("Error processing signal through OrderManager pipeline")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Execution pipeline error: {exc}",
            ) from exc

    return {"status": "received", "source": "tradingview"}
