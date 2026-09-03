"""TradingView Webhook router definition."""

import asyncio
import csv
import hmac
import json
import logging
import threading
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

# TEMPORARY: append-only CSV of every accepted webhook. Remove later.
INCOMING_SIGNALS_CSV_NAME = "incoming_signals.csv"
_CSV_LOCK = threading.Lock()
_INCOMING_SIGNAL_CSV_FIELDS = (
    "received_at",
    "request_id",
    "signal_id",
    "trade_id",
    "job_id",
    "duplicate",
    "strategy",
    "action",
    "direction",
    "market",
    "ts",
    "leg_a_symbol",
    "leg_a_side",
    "leg_a_weight",
    "leg_a_price",
    "leg_a_instrument_type",
    "leg_b_symbol",
    "leg_b_side",
    "leg_b_weight",
    "leg_b_price",
    "leg_b_instrument_type",
    "raw_json",
)


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _bucket_leg_csv_fields(payload: dict[str, Any], index: int, prefix: str) -> dict[str, str]:
    empty = {
        f"{prefix}_symbol": "",
        f"{prefix}_side": "",
        f"{prefix}_weight": "",
        f"{prefix}_price": "",
        f"{prefix}_instrument_type": "",
    }
    buckets = payload.get("buckets")
    if not isinstance(buckets, list) or index >= len(buckets):
        return empty
    bucket = buckets[index]
    if not isinstance(bucket, dict):
        return empty
    legs = bucket.get("legs")
    leg = legs[0] if isinstance(legs, list) and legs and isinstance(legs[0], dict) else {}
    return {
        f"{prefix}_symbol": _csv_cell(bucket.get("underlying")),
        f"{prefix}_side": _csv_cell(leg.get("side")),
        f"{prefix}_weight": _csv_cell(leg.get("weight")),
        f"{prefix}_price": _csv_cell(leg.get("price")),
        f"{prefix}_instrument_type": _csv_cell(leg.get("instrument_type")),
    }


def _incoming_signal_csv_row(
    *,
    payload: dict[str, Any],
    received_at: str,
    request_id: str,
    signal_id: str,
    trade_id: str | None,
    job_id: str,
    duplicate: bool,
) -> dict[str, str]:
    """Flatten an accepted webhook into one CSV row. TEMPORARY."""
    row = {
        "received_at": received_at,
        "request_id": request_id,
        "signal_id": signal_id,
        "trade_id": _csv_cell(trade_id),
        "job_id": job_id,
        "duplicate": "true" if duplicate else "false",
        "strategy": _csv_cell(payload.get("strategy") or payload.get("strategy_id")),
        "action": _csv_cell(payload.get("action")),
        "direction": _csv_cell(payload.get("direction")),
        "market": _csv_cell(payload.get("market")),
        "ts": _csv_cell(payload.get("ts") or payload.get("time")),
        "raw_json": json.dumps(payload, separators=(",", ":")),
    }
    row.update(_bucket_leg_csv_fields(payload, 0, "leg_a"))
    row.update(_bucket_leg_csv_fields(payload, 1, "leg_b"))
    return row


def _append_incoming_signal_csv(row: dict[str, str]) -> None:
    """Append one accepted-signal row to the temporary CSV (process-local lock)."""
    WEBHOOK_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = WEBHOOK_CAPTURE_DIR / INCOMING_SIGNALS_CSV_NAME
    with _CSV_LOCK:
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=_INCOMING_SIGNAL_CSV_FIELDS,
                extrasaction="ignore",
            )
            if new_file:
                writer.writeheader()
            writer.writerow(row)


def _verify_webhook_authentication(request: Request) -> None:
    """Validate webhook authentication secret from X-Webhook-Secret header using constant-time comparison."""
    settings = get_settings()
    if not settings.webhook_auth_enabled:
        logger.info("Webhook authentication is disabled (WEBHOOK_AUTH_ENABLED=false)")
        return

    expected_secret = settings.webhook_auth_secret
    if not expected_secret:
        logger.warning(
            "Webhook request rejected: WEBHOOK_AUTH_SECRET is not configured."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Webhook authentication not configured.",
        )

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
        job_id_str = ""
        created_any = False
        async with session_factory() as session, session.begin():
            from app.accounts.router import DatabaseStrategyAccountRouter

            scopes: list[str | None] = [None]
            try:
                contexts = await DatabaseStrategyAccountRouter(session_factory).resolve(
                    strategy_id, session=session
                )
                if contexts:
                    scopes = [str(ctx.account_id) for ctx in contexts]
            except Exception:
                logger.exception(
                    "Account router failed during ingest; enqueueing unscoped job strategy_id=%s",
                    strategy_id,
                )

            for scope in scopes:
                scoped_key = (
                    idempotency_key
                    if scope is None
                    else f"{idempotency_key}:{scope}"
                )
                job, created = await SignalJobRepository(session).create_job_if_not_exists(
                    signal_id=signal_id,
                    strategy_id=strategy_id,
                    trade_id=trade_id,
                    idempotency_key=scoped_key,
                    raw_payload=payload,
                    capture_data=capture_data,
                    correlation_id=request_id,
                    account_scope=scope,
                )
                job_id_str = str(job.job_id)
                created_any = created_any or created
                if not created:
                    logger.info(
                        "Duplicate webhook received for idempotency_key=%s signal_id=%s "
                        "account_scope=%s (job_id=%s)",
                        scoped_key,
                        signal_id,
                        scope,
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

    # TEMPORARY: also dump every accepted signal to CSV. Remove later.
    try:
        await asyncio.to_thread(
            _append_incoming_signal_csv,
            _incoming_signal_csv_row(
                payload=payload,
                received_at=received_at,
                request_id=request_id,
                signal_id=signal_id,
                trade_id=trade_id,
                job_id=job_id_str,
                duplicate=not created,
            ),
        )
    except Exception:
        logger.exception("TEMPORARY: failed to append incoming signal CSV row")

    return TradingViewWebhookResponse(
        status="accepted",
        source="tradingview",
        signal_id=signal_id,
        job_id=job_id_str,
        request_id=request_id,
    )


