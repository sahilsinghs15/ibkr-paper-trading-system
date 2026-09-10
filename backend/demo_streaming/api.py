"""Read-only SSE API for the position demo. Does not trade."""

import asyncio
import csv
import io
import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

try:
    from datetime import UTC  # Python 3.11+
except ImportError:  # pragma: no cover
    UTC = timezone.utc  # type: ignore[assignment]

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import running_under_pytest
from app.core.security import decode_access_token, decode_sse_token
from app.db.models.account import AccountModel
from app.db.models.event import EventLogModel
from app.db.models.notification_read import (
    UserNotificationReadModel,
    UserNotificationStateModel,
)
from app.db.models.position import PositionModel
from app.db.models.user import UserModel
from app.db.repositories.event_repository import EventRepository
from app.services.notification_canonical import (
    ALLOWED_KINDS,
    ALLOWED_SERVICES,
    format_canonical_notification,
)
from demo_streaming.snapshot import (
    load_baskets,
    load_closed_position_rows,
    load_orders,
    load_pair_detail,
    load_position_rows,
    load_signals,
    position_leg_payloads,
)
from demo_streaming.stream import PositionStream

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Vite build output: app/frontend/dist (repo layout: backend/../frontend/dist)
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
# Match demo_poll_interval_ms (2s): Redis XREAD wait between SSE keepalives.
SSE_BLOCK_MS = 2000


async def _get_authenticated_user_from_request(
    request: Request, session_factory: async_sessionmaker[AsyncSession]
) -> UserModel | None:
    query_token = request.query_params.get("token")
    auth_header = request.headers.get("Authorization")
    header_token = (
        auth_header[7:].strip()
        if (auth_header and auth_header.startswith("Bearer "))
        else None
    )

    token_payload = None

    if query_token:
        # Query parameter MUST be a short-lived SSE token! Rejects normal access tokens.
        try:
            token_payload = decode_sse_token(query_token)
        except jwt.PyJWTError:
            return None
    elif header_token:
        # Header token accepts ONLY normal access JWT tokens for REST endpoints
        try:
            token_payload = decode_access_token(header_token)
        except jwt.PyJWTError:
            return None
    else:
        if os.environ.get("TRADINGAPP_TESTING") == "1" and running_under_pytest():
            return UserModel(
                id=999999,
                email="test_admin@example.com",
                password_hash="mock",
                role="admin",
                is_active=True,
                ibkr_account_id=None,
            )
        return None

    if not token_payload or not token_payload.get("sub"):
        return None

    try:
        user_id = int(token_payload["sub"])
    except (ValueError, TypeError):
        return None

    async with session_factory() as session:
        result = await session.execute(
            select(UserModel)
            .options(selectinload(UserModel.account))
            .where(UserModel.id == user_id)
        )
        user = result.scalar_one_or_none()
        if user and user.is_active:
            return user
    return None


def _spa_index() -> FileResponse:
    react_index = FRONTEND_DIST / "index.html"
    headers = {"Cache-Control": "no-store"}
    if react_index.is_file():
        return FileResponse(react_index, headers=headers)
    return FileResponse(STATIC_DIR / "index.html", headers=headers)


def create_demo_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    stream_name: str,
    trading_api_url: str = "http://127.0.0.1:8001",
    shutdown: asyncio.Event | None = None,
) -> FastAPI:
    from app.core.config import refuse_testing_flag_on_order_process

    refuse_testing_flag_on_order_process()
    stream = PositionStream(redis, stream_name)
    stop = shutdown or asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.shutdown = stop
        yield
        stop.set()

    app = FastAPI(
        title="Position demo stream",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    dist_assets = FRONTEND_DIST / "assets"
    if dist_assets.is_dir():
        app.mount("/assets", StaticFiles(directory=dist_assets), name="frontend-assets")

    @app.get("/health")
    async def health() -> dict:
        redis_ok = False
        try:
            redis_ok = await stream.ping()
        except Exception:
            logger.exception("Redis ping failed")
        return {
            "status": "ok" if redis_ok else "degraded",
            "redis": redis_ok,
            "stream": stream.stream_name,
            "mode": "read-only",
        }

    @app.get("/demo/positions")
    async def positions(request: Request) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        now = datetime.now(UTC)
        async with session_factory() as session:
            rows = await load_position_rows(session)
            if user.role == "user":
                rows = [r for r in rows if r[0].account_id == user.ibkr_account_id]
            keys = {
                (position.account_id, position.trade_id) for position, _account in rows
            }
            baskets = await load_baskets(session, keys)
            orders = await load_orders(session, keys)
        payload = []
        for position, account in rows:
            if position.risk_state != "OPEN":
                continue
            key = (position.account_id, position.trade_id)
            payload.extend(
                position_leg_payloads(
                    position,
                    account,
                    baskets.get(key, []),
                    orders.get(key, []),
                    timestamp=now,
                )
            )
        return JSONResponse({"positions": payload, "market_data_status": "UNAVAILABLE"})

    @app.get("/demo/positions/{account_id}/{trade_id}")
    async def position_detail(
        request: Request, account_id: int, trade_id: str
    ) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user.role == "user" and account_id != user.ibkr_account_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        from app.core.config import get_settings

        settings = get_settings()
        async with session_factory() as session:
            payload = await load_pair_detail(session, account_id, trade_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Position not found")
        payload["exits"]["monitor_enabled"] = settings.risk_exit_monitor_enabled
        payload["exits"]["shadow_mode"] = settings.risk_exit_shadow_mode
        return JSONResponse(payload)

    @app.get("/demo/closed-positions")
    async def closed_positions(
        request: Request, account_id: int | None = None
    ) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user.role == "user":
            account_id = user.ibkr_account_id
        now = datetime.now(UTC)
        async with session_factory() as session:
            rows = await load_closed_position_rows(session, account_id=account_id)
            keys = {
                (position.account_id, position.trade_id) for position, _account in rows
            }
            baskets = await load_baskets(session, keys)
            orders = await load_orders(session, keys)
        payload = []
        for position, account in rows:
            key = (position.account_id, position.trade_id)
            payload.extend(
                position_leg_payloads(
                    position,
                    account,
                    baskets.get(key, []),
                    orders.get(key, []),
                    timestamp=now,
                )
            )
        return JSONResponse({"closed_positions": payload})

    @app.get("/demo/closed-positions/csv")
    async def closed_positions_csv(
        request: Request,
        account_id: int | None = None,
        ibkr_account: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> StreamingResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user.role == "user":
            account_id = user.ibkr_account_id

        def _parse_dt(val: str | None) -> datetime | None:
            if not val:
                return None
            try:
                dt = datetime.fromisoformat(val)
                # Ensure timezone-aware for comparison with DB timestamptz
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                return None

        parsed_from = _parse_dt(date_from)
        parsed_to = _parse_dt(date_to)

        account_label = "ALL"
        async with session_factory() as session:
            if ibkr_account and account_id is None:
                acc_row = (
                    await session.execute(
                        select(AccountModel).where(
                            func.upper(AccountModel.ibkr_account)
                            == ibkr_account.strip().upper()
                        )
                    )
                ).scalar_one_or_none()
                if acc_row:
                    account_id = acc_row.id
                    account_label = acc_row.ibkr_account
                else:
                    account_id = -1
                    # Sanitize user-supplied label for filename
                    account_label = "".join(
                        c for c in ibkr_account.strip().upper() if c.isalnum() or c in ("-", "_")
                    ) or "UNKNOWN"
            elif account_id is not None:
                acc_row = (
                    await session.execute(
                        select(AccountModel).where(AccountModel.id == account_id)
                    )
                ).scalar_one_or_none()
                if acc_row:
                    account_label = acc_row.ibkr_account

        async def csv_generator() -> AsyncGenerator[str, None]:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            headers = [
                "Trade ID",
                "Account",
                "Strategy",
                "Leg A Symbol",
                "Leg A Type",
                "Leg A Qty",
                "Leg A Entry Mark",
                "Leg B Symbol",
                "Leg B Type",
                "Leg B Qty",
                "Leg B Entry Mark",
                "Realized PnL",
                "Commission",
                "Net PnL",
                "Exit Reason",
                "Opened At",
                "Closed At",
                "Duration (Days)",
            ]
            writer.writerow(headers)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

            batch_size = 500
            offset = 0
            while True:
                async with session_factory() as session:
                    stmt = (
                        select(PositionModel, AccountModel)
                        .join(AccountModel, AccountModel.id == PositionModel.account_id)
                        .where(PositionModel.risk_state == "CLOSED")
                    )
                    if account_id is not None:
                        stmt = stmt.where(PositionModel.account_id == account_id)
                    if parsed_from is not None:
                        stmt = stmt.where(PositionModel.closed_at >= parsed_from)
                    if parsed_to is not None:
                        stmt = stmt.where(PositionModel.closed_at <= parsed_to)
                    stmt = stmt.order_by(
                        PositionModel.closed_at.desc(),
                        PositionModel.account_id,
                        PositionModel.trade_id,
                    )
                    stmt = stmt.limit(batch_size).offset(offset)
                    result = await session.execute(stmt)
                    rows = list(result.all())

                if not rows:
                    break

                for pos, acc in rows:
                    opened_s = pos.opened_at.isoformat() if pos.opened_at else ""
                    closed_s = pos.closed_at.isoformat() if pos.closed_at else ""
                    duration_days = ""
                    if pos.opened_at and pos.closed_at:
                        diff = pos.closed_at - pos.opened_at
                        duration_days = f"{diff.total_seconds() / 86400.0:.2f}"

                    net_pnl = (pos.realised_pnl or Decimal(0)) - (
                        pos.commission or Decimal(0)
                    )

                    writer.writerow(
                        [
                            pos.trade_id,
                            acc.ibkr_account,
                            pos.strategy_id,
                            pos.leg_a_symbol,
                            pos.leg_a_instrument_type,
                            str(pos.leg_a_signed_qty),
                            str(pos.leg_a_entry_mark),
                            pos.leg_b_symbol or "",
                            pos.leg_b_instrument_type or "",
                            str(pos.leg_b_signed_qty)
                            if pos.leg_b_signed_qty is not None
                            else "",
                            str(pos.leg_b_entry_mark)
                            if pos.leg_b_entry_mark is not None
                            else "",
                            str(pos.realised_pnl),
                            str(pos.commission),
                            str(net_pnl),
                            pos.exit_reason or "",
                            opened_s,
                            closed_s,
                            duration_days,
                        ]
                    )
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

                offset += len(rows)
                if len(rows) < batch_size:
                    break

        timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        filename = f"closed_trades_{account_label}_{timestamp_str}.csv"
        return StreamingResponse(
            csv_generator(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/demo/audit-logs")
    async def audit_logs(
        request: Request,
        category: str | None = None,
        process: str | None = None,
        kind: str | None = None,
        search: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user.role != "admin":
            raise HTTPException(
                status_code=403, detail="Admin role required for audit logs"
            )

        def _parse_audit_dt(val: str | None) -> datetime | None:
            if not val:
                return None
            try:
                dt = datetime.fromisoformat(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                return None

        parsed_from = _parse_audit_dt(date_from)
        parsed_to = _parse_audit_dt(date_to)

        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        async with session_factory() as session:
            repo = EventRepository(session)
            rows, total = await repo.query_events(
                category=category,
                process=process,
                kind=kind,
                search=search,
                date_from=parsed_from,
                date_to=parsed_to,
                limit=limit,
                offset=offset,
            )

        events_payload = [
            {
                "id": r.id,
                "ts": r.ts.isoformat(),
                "process": r.process,
                "kind": r.kind,
                "signal_id": r.signal_id,
                "order_id": r.order_id,
                "basket_id": r.basket_id,
                "idempotency_key": r.idempotency_key,
                "detail": r.detail,
            }
            for r in rows
        ]

        return JSONResponse(
            {
                "total": total,
                "limit": limit,
                "offset": offset,
                "events": events_payload,
            }
        )

    @app.get("/demo/signals")
    async def signals(
        request: Request,
        limit: int | None = None,
        page: int = 1,
        page_size: int = 100,
        status: str | None = None,
        account_id: int | None = None,
        ibkr_account: str | None = None,
    ) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        logger.info(
            "/demo/signals authenticated user_id=%s role=%s", user.id, user.role
        )
        if user.role == "user":
            account_id = user.ibkr_account_id
            ibkr_account = user.account.ibkr_account if user.account else None
        async with session_factory() as session:
            payload = await load_signals(
                session,
                limit=limit,
                page=page,
                page_size=page_size,
                status_filter=status,
                account_id=account_id,
                ibkr_account=ibkr_account,
                return_dict=True,
            )
        if isinstance(payload, list):
            return JSONResponse({"signals": payload})
        return JSONResponse(payload)

    @app.get("/demo/market-data-health")
    async def get_market_data_health() -> JSONResponse:
        pnl_svc = getattr(app.state, "live_pnl_service", None)
        if pnl_svc is not None and hasattr(pnl_svc, "get_market_data_health"):
            return JSONResponse(pnl_svc.get_market_data_health())
        return JSONResponse(
            {"status": "unavailable", "detail": "LivePnlService not initialized"}
        )

    @app.get("/demo/system-events")
    async def get_system_events(
        request: Request,
        since_id: int = 0,
        limit: int = 20,
    ) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        if since_id < 0:
            raise HTTPException(status_code=422, detail="since_id must be non-negative")
        clamped_limit = min(max(1, limit), 100)

        async with session_factory() as session:
            stmt = (
                select(EventLogModel)
                .where(
                    EventLogModel.kind.in_(ALLOWED_KINDS),
                    EventLogModel.id > since_id,
                )
                .order_by(EventLogModel.id.asc())
                .limit(clamped_limit)
            )
            rows = (await session.execute(stmt)).scalars().all()

        events_payload = []
        for row in rows:
            if row.kind in ("SERVICE_STARTED", "SERVICE_STOPPED"):
                svc = (row.detail or {}).get("service")
                if svc not in ALLOWED_SERVICES:
                    continue
            canonical = format_canonical_notification(row.kind, row.detail)
            events_payload.append(
                {
                    "id": row.id,
                    "ts": row.ts.isoformat() if row.ts else None,
                    "kind": row.kind,
                    "service": canonical["service"],
                    "unit": canonical["unit"],
                    "friendly_name": canonical["friendly_name"],
                    "title": canonical["title"],
                    "message": canonical["message"],
                    "icon": canonical["icon"],
                    "detail": row.detail,
                }
            )
        return JSONResponse(events_payload)

    @app.get("/demo/notifications")
    async def get_notifications(
        request: Request,
        limit: int = 30,
        offset: int = 0,
    ) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        clamped_limit = min(max(1, limit), 100)
        safe_offset = max(0, offset)

        async with session_factory() as session:
            # 1. User read state
            state_stmt = select(UserNotificationStateModel).where(
                UserNotificationStateModel.user_id == user.id
            )
            user_state = (await session.execute(state_stmt)).scalar_one_or_none()
            last_read_all_id = user_state.last_read_all_id if user_state else 0

            reads_stmt = select(UserNotificationReadModel.event_id).where(
                UserNotificationReadModel.user_id == user.id
            )
            individual_read_ids = set(
                (await session.execute(reads_stmt)).scalars().all()
            )

            # 2. Approved historical notifications
            stmt = (
                select(EventLogModel)
                .where(EventLogModel.kind.in_(ALLOWED_KINDS))
                .order_by(EventLogModel.id.desc())
            )
            all_rows = (await session.execute(stmt)).scalars().all()

            # Filter strictly to approved scope
            approved_rows = []
            for r in all_rows:
                if (
                    r.kind in ("SERVICE_STARTED", "SERVICE_STOPPED")
                    and (r.detail or {}).get("service") not in ALLOWED_SERVICES
                ):
                    continue
                approved_rows.append(r)

            total = len(approved_rows)
            paged_rows = approved_rows[safe_offset : safe_offset + clamped_limit]

            # 3. Unread count
            unread_count = sum(
                1
                for r in approved_rows
                if r.id > last_read_all_id and r.id not in individual_read_ids
            )

            items = []
            for row in paged_rows:
                canonical = format_canonical_notification(row.kind, row.detail)
                is_read = (row.id <= last_read_all_id) or (
                    row.id in individual_read_ids
                )
                items.append(
                    {
                        "id": row.id,
                        "ts": row.ts.isoformat() if row.ts else None,
                        "kind": row.kind,
                        "service": canonical["service"],
                        "unit": canonical["unit"],
                        "friendly_name": canonical["friendly_name"],
                        "title": canonical["title"],
                        "message": canonical["message"],
                        "icon": canonical["icon"],
                        "is_read": is_read,
                        "detail": row.detail,
                    }
                )

        return JSONResponse(
            {
                "items": items,
                "unread_count": unread_count,
                "total": total,
            }
        )

    @app.post("/demo/notifications/{event_id}/read")
    async def mark_notification_read(
        request: Request,
        event_id: int,
    ) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        async with session_factory() as session:
            stmt = select(UserNotificationReadModel).where(
                UserNotificationReadModel.user_id == user.id,
                UserNotificationReadModel.event_id == event_id,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if not existing:
                session.add(
                    UserNotificationReadModel(user_id=user.id, event_id=event_id)
                )
                await session.commit()

            # Recalculate unread count
            state_stmt = select(UserNotificationStateModel).where(
                UserNotificationStateModel.user_id == user.id
            )
            user_state = (await session.execute(state_stmt)).scalar_one_or_none()
            last_read_all_id = user_state.last_read_all_id if user_state else 0

            reads_stmt = select(UserNotificationReadModel.event_id).where(
                UserNotificationReadModel.user_id == user.id
            )
            individual_read_ids = set(
                (await session.execute(reads_stmt)).scalars().all()
            )

            rows_stmt = select(EventLogModel).where(
                EventLogModel.kind.in_(ALLOWED_KINDS),
                EventLogModel.id > last_read_all_id,
            )
            rows = (await session.execute(rows_stmt)).scalars().all()
            unread_count = sum(
                1
                for r in rows
                if (
                    r.kind == "MARKET_CLOSED"
                    or (r.detail or {}).get("service") in ALLOWED_SERVICES
                )
                and r.id not in individual_read_ids
            )

        return JSONResponse(
            {"ok": True, "event_id": event_id, "unread_count": unread_count}
        )

    @app.post("/demo/notifications/mark-all-read")
    async def mark_all_notifications_read(
        request: Request,
    ) -> JSONResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        async with session_factory() as session:
            max_stmt = select(func.max(EventLogModel.id)).where(
                EventLogModel.kind.in_(ALLOWED_KINDS)
            )
            max_id = (await session.execute(max_stmt)).scalar() or 0

            state_stmt = select(UserNotificationStateModel).where(
                UserNotificationStateModel.user_id == user.id
            )
            user_state = (await session.execute(state_stmt)).scalar_one_or_none()
            if user_state:
                user_state.last_read_all_id = max(user_state.last_read_all_id, max_id)
            else:
                session.add(
                    UserNotificationStateModel(user_id=user.id, last_read_all_id=max_id)
                )
            await session.commit()

        return JSONResponse({"ok": True, "last_read_id": max_id, "unread_count": 0})

    @app.get("/demo/stream")
    async def sse(request: Request) -> StreamingResponse:
        user = await _get_authenticated_user_from_request(request, session_factory)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        user_role = user.role
        user_ibkr_acc = user.account.ibkr_account if user.account else None
        user_acc_id = user.ibkr_account_id

        async def events():
            logger.info(
                "SSE client connected: stream=%s user_id=%s role=%s",
                stream.stream_name,
                user.id,
                user_role,
            )
            try:
                yield _sse({"event": "hello", "stream": stream.stream_name})
                last_id = "$"
                while not stop.is_set():
                    if await request.is_disconnected():
                        break
                    try:
                        entries = await stream.xread(
                            last_id, block_ms=SSE_BLOCK_MS, count=20
                        )
                        if stop.is_set() or await request.is_disconnected():
                            break
                        if not entries:
                            yield ": keepalive\n\n"
                            continue
                        for entry_id, fields in entries:
                            last_id = entry_id
                            if user_role == "user":
                                f_acc = fields.get("ibkr_account") or fields.get(
                                    "account"
                                )
                                f_acc_id = fields.get("account_id")
                                if (
                                    f_acc
                                    and str(f_acc).strip().upper()
                                    != (user_ibkr_acc or "").strip().upper()
                                ):
                                    continue
                                if (
                                    f_acc_id
                                    and user_acc_id
                                    and int(f_acc_id) != user_acc_id
                                ):
                                    continue
                            yield _sse(fields | {"redis_id": entry_id})
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("SSE redis read failed")
                        yield _sse(
                            {
                                "event": "stream_error",
                                "market_data_status": "UNAVAILABLE",
                            }
                        )
                        await asyncio.sleep(1)
            finally:
                logger.info("SSE client disconnected: stream=%s", stream.stream_name)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.api_route(
        "/api/v1/{full_path:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    )
    async def proxy_trading_api(request: Request, full_path: str) -> Response:
        """Forward /api/v1/* requests to the trading API for same-origin dashboard access."""
        api_base = trading_api_url.rstrip("/") + "/api/v1"
        url = f"{api_base}/{full_path}" if full_path else api_base
        if request.url.query:
            url = f"{url}?{request.url.query}"
        body = await request.body()
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "connection")
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                upstream = await client.request(
                    request.method,
                    url,
                    content=body if body else None,
                    headers=headers,
                )
        except httpx.RequestError as exc:
            logger.exception("Trading API proxy failed: url=%s", url)
            raise HTTPException(
                status_code=502, detail=f"Trading API unreachable: {exc}"
            ) from exc
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    @app.get("/")
    @app.get("/login")
    @app.get("/accounts")
    @app.get("/settings")
    @app.get("/system-monitor")
    @app.get("/audit-logs")
    @app.get("/trade-book")
    @app.get("/account/{path:path}")
    async def index() -> FileResponse:
        return _spa_index()

    @app.get("/favicon.svg")
    async def favicon() -> FileResponse:
        react_icon = FRONTEND_DIST / "favicon.svg"
        if react_icon.is_file():
            return FileResponse(react_icon)
        raise HTTPException(status_code=404)

    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"
