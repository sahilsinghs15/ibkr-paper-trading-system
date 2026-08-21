"""TradingView Webhook router definition."""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.core.logger import bind_log_context, clear_log_context
from app.db.repositories.signal_repository import SignalJobRepository
from app.schemas.webhook import TradingViewWebhookResponse
from app.services.strategies.inbound import parse_tradingview_payload
from app.services.worker_pool import compute_idempotency_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

WEBHOOK_CAPTURE_DIR = (
    Path(__file__).resolve().parents[3] / "data" / "tradingview_webhooks"
)


def _save_raw_capture_file(capture_data: dict[str, Any], filename: str) -> None:
    """Save raw capture JSON payload to disk off the FastAPI event loop."""
    WEBHOOK_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    file_path = WEBHOOK_CAPTURE_DIR / filename
    file_path.write_text(json.dumps(capture_data, indent=2), encoding="utf-8")


@router.post(
    "/tradingview",
    response_model=TradingViewWebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive TradingView Webhook",
    description="Fast ingestion endpoint to validate, capture, and durably queue TradingView alerts.",
)
async def receive_tradingview_webhook(request: Request) -> TradingViewWebhookResponse:
    """Ingest, validate, safely log, locally capture, and persist a TradingView JSON alert payload into PostgreSQL durable queue."""
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
    bind_log_context(request_id=request_id)
    try:
        return await _process_tradingview_webhook(
            request, payload, raw_body=raw_body, request_id=request_id
        )
    finally:
        clear_log_context()


async def _process_tradingview_webhook(
    request: Request,
    payload: dict[str, Any],
    *,
    raw_body: str,
    request_id: str,
) -> TradingViewWebhookResponse:
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

    # Non-blocking disk persistence off the event loop
    await asyncio.to_thread(_save_raw_capture_file, capture_data, filename)

    strategy_id, signal_id, trade_id, idempotency_key = compute_idempotency_key(payload)
    bind_log_context(signal_id=signal_id, trade_id=trade_id or signal_id)

    order_manager = getattr(request.app.state, "order_manager", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None and order_manager is not None:
        session_factory = getattr(order_manager, "_session_factory", None)

    # Validate signal payload structure via strategy parser
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
        if order_manager is not None:
            await order_manager.record_rejected_inbound(
                payload, capture_data=capture_data, reason=str(val_err)
            )
        return TradingViewWebhookResponse(
            status="rejected",
            source="tradingview",
            signal_id=signal_id,
            request_id=request_id,
        )

    job_id_str = None
    if session_factory is not None:
        try:
            async with session_factory() as session, session.begin():
                job, created = await SignalJobRepository(session).create_job_if_not_exists(
                    signal_id=domain_signal.signal_id,
                    strategy_id=domain_signal.strategy_id or strategy_id,
                    trade_id=domain_signal.trade_id or trade_id,
                    idempotency_key=idempotency_key,
                    raw_payload=payload,
                    capture_data=capture_data,
                    correlation_id=request_id,
                )
                job_id_str = str(job.job_id)
                if not created:
                    logger.info(
                        "Duplicate webhook received for idempotency_key=%s signal_id=%s (job_id=%s)",
                        idempotency_key,
                        domain_signal.signal_id,
                        job_id_str,
                    )
        except Exception:
            logger.exception("Failed to persist durable signal job into PostgreSQL queue")

    # If legacy in-line execution mode is required (e.g. worker pool not running and order_manager present)
    worker_pool = getattr(request.app.state, "worker_pool", None)
    if worker_pool is None and order_manager is not None and session_factory is None:
        try:
            execution = await order_manager.process_signal_execution(domain_signal)
            if execution is not None and getattr(execution, "all_rejected", False) and not execution.orders:
                return TradingViewWebhookResponse(
                    status="rejected_by_rms",
                    source="tradingview",
                    signal_id=domain_signal.signal_id,
                    request_id=request_id,
                )
        except ValueError:
            return TradingViewWebhookResponse(
                status="rejected_by_rms",
                source="tradingview",
                signal_id=domain_signal.signal_id,
                request_id=request_id,
            )

    logger.info(
        "Webhook HTTP 202 accepted: signal_id=%s job_id=%s request_id=%s",
        domain_signal.signal_id,
        job_id_str,
        request_id,
    )
    return TradingViewWebhookResponse(
        status="accepted",
        source="tradingview",
        signal_id=domain_signal.signal_id,
        job_id=job_id_str,
        request_id=request_id,
    )

