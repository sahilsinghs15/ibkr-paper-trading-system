"""TradingView Webhook router definition."""

import asyncio
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import get_settings
from app.core.logger import bind_log_context, clear_log_context
from app.db.repositories.signal_repository import SignalJobRepository
from app.schemas.webhook import TradingViewWebhookResponse
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


def _verify_webhook_authentication(request: Request) -> None:
    """Validate webhook authentication secret from X-Webhook-Secret header using constant-time comparison."""
    settings = get_settings()
    expected_secret = settings.webhook_auth_secret

    if expected_secret:
        incoming_secret = request.headers.get("X-Webhook-Secret")
        if not incoming_secret or not hmac.compare_digest(
            expected_secret.encode("utf-8"), incoming_secret.encode("utf-8")
        ):
            logger.warning("Unauthorized webhook request: missing or invalid X-Webhook-Secret header")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Missing or invalid authentication secret.",
            )



@router.post(
    "/tradingview",
    response_model=TradingViewWebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive TradingView Webhook",
    description="Fast, secure ingestion endpoint to authenticate and durably queue TradingView alerts.",
)
async def receive_tradingview_webhook(request: Request) -> TradingViewWebhookResponse:
    """Ingest, authenticate, validate JSON, compute idempotency key, and persist into PostgreSQL queue."""
    # Enforce authentication BEFORE any database access or payload processing
    _verify_webhook_authentication(request)

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

    strategy_id, signal_id, trade_id, idempotency_key = compute_idempotency_key(payload)
    bind_log_context(signal_id=signal_id, trade_id=trade_id or signal_id)

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        order_manager = getattr(request.app.state, "order_manager", None)
        if order_manager is not None:
            session_factory = getattr(order_manager, "_session_factory", None)

    if session_factory is None:
        logger.error("Database session factory unavailable; cannot persist signal job")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="System unavailable: Database session factory not configured.",
        )

    try:
        async with session_factory() as session, session.begin():
            job, created = await SignalJobRepository(session).create_job_if_not_exists(
                signal_id=signal_id,
                strategy_id=strategy_id,
                trade_id=trade_id,
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
                    signal_id,
                    job_id_str,
                )
    except Exception as exc:
        logger.exception("Failed to persist durable signal job into PostgreSQL queue")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to durably persist signal job.",
        ) from exc

    logger.info(
        "Webhook HTTP 202 accepted: signal_id=%s job_id=%s request_id=%s",
        signal_id,
        job_id_str,
        request_id,
    )
    return TradingViewWebhookResponse(
        status="accepted",
        source="tradingview",
        signal_id=signal_id,
        job_id=job_id_str,
        request_id=request_id,
    )


