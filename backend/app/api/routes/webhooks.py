"""TradingView Webhook router definition."""

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.models.signal import Signal, SignalType
from app.schemas.webhook import TradingViewWebhookResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

WEBHOOK_CAPTURE_DIR = (
    Path(__file__).resolve().parents[3] / "data" / "tradingview_webhooks"
)


def _extract_decimal(val: Any, default: Decimal = Decimal(0)) -> Decimal:
    """Safely extract a Decimal value from payload data."""
    if val is None:
        return default
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return default


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

    # Signal field extraction
    strategy_id = str(
        payload.get("strategy")
        or payload.get("strategy_id")
        or "default_strategy"
    )
    raw_signal_id = (
        payload.get("signal_id")
        or payload.get("trade_id")
        or request_id
    )
    signal_id = str(raw_signal_id)

    action = str(
        payload.get("action")
        or payload.get("signal_type")
        or "OPEN"
    ).upper()
    pair = str(
        payload.get("pair")
        or payload.get("ticker")
        or payload.get("symbol")
        or "N/A"
    )
    side = str(
        payload.get("side")
        or payload.get("direction")
        or "BUY"
    ).upper()

    ref_price_a = _extract_decimal(
        payload.get("ref_price_a", payload.get("price"))
    )
    raw_price_b = payload.get("ref_price_b")
    ref_price_b = (
        _extract_decimal(raw_price_b) if raw_price_b is not None else None
    )

    raw_qty = payload.get("quantity") or payload.get("qty") or payload.get("position_size")
    quantity: int | None = None
    if raw_qty is not None:
        try:
            quantity = int(float(str(raw_qty)))
        except (ValueError, TypeError):
            quantity = None

    # Map payload to domain Signal model
    sig_type_str = str(payload.get("action") or payload.get("signal_type") or "BUY").upper()
    if sig_type_str in ("HOLD",):
        sig_type = SignalType.HOLD
    elif sig_type_str in ("SELL", "SHORT", "CLOSE"):
        sig_type = SignalType.SELL
    else:
        sig_type = SignalType.BUY

    domain_signal = Signal(
        signal_type=sig_type,
        timestamp=utc_now,
        reason=f"TradingView webhook request_id={request_id}",
        signal_id=signal_id,
        strategy_id=strategy_id,
        action=action,
        symbol=pair if pair != "N/A" else None,
        side=side,
        price=ref_price_a if ref_price_a > 0 else None,
        quantity=quantity,
        raw_payload=capture_data,
    )

    # Process signal through OrderManager -> RMS -> OMS pipeline
    order_manager = getattr(request.app.state, "order_manager", None)
    if order_manager is not None:
        try:
            order = await order_manager.process_signal(domain_signal)
            if order is not None:
                logger.info(
                    "Signal successfully processed by OrderManager -> RMS -> OMS: order_id=%s, ibkr_id=%s",
                    order.internal_order_id,
                    order.ibkr_order_id,
                )
        except ValueError as val_err:
            logger.warning("RMS check rejected incoming TradingView signal: %s", val_err)
            return {"status": "rejected_by_rms", "source": "tradingview"}
        except (ConnectionError, RuntimeError) as conn_err:
            logger.warning("Signal ingested but broker submission unconfirmed: %s", conn_err)
            return {"status": "received", "source": "tradingview"}
        except Exception as exc:
            logger.exception("Error processing signal through OrderManager pipeline: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Execution pipeline error: {exc}",
            ) from exc

    return {"status": "received", "source": "tradingview"}



