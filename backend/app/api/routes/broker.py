"""Broker execution snapshot API endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import require_authenticated_user
from app.api.routes.config import _check_account_authorization
from app.db.models.user import UserModel
from sqlalchemy import func, select

from app.db.models.account import AccountModel
from app.db.models.trade_execution import TradeExecutionModel
from app.db.session import get_db_session
from app.schemas.broker_execution_schemas import (
    BrokerExecutionLineSchema,
    BrokerExecutionsResponse,
    OrderBookResponse,
    OrderBookRowSchema,
    TradeBookPaginatedResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/broker", tags=["broker"])


@router.get(
    "/executions",
    summary="Get today's IBKR Gateway executions snapshot",
    description=(
        "Retrieve today's broker executions (since midnight) directly from the active "
        "IBKR Gateway via TWSClient reqExecutions. Display-only snapshot; not persisted to DB."
    ),
    response_model=BrokerExecutionsResponse,
)
async def get_broker_executions(
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[
        str,
        Query(
            ...,
            description="Mandatory IBKR account code (e.g. DUR919062)",
            min_length=1,
        ),
    ],
) -> BrokerExecutionsResponse:
    """Return live execution snapshot since midnight for the specified account."""
    clean_account = ibkr_account.strip().upper()
    if not clean_account:
        raise HTTPException(status_code=400, detail="ibkr_account cannot be empty")

    _check_account_authorization(current_user, ibkr_account=clean_account)

    client = getattr(request.app.state, "client", None)
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="TWS gateway is down.")

    lines, timed_out = await client.request_executions_async(
        ibkr_account=clean_account,
        timeout=15.0,
    )

    # Defense-in-depth: only retain executions strictly matching the requested account
    filtered_lines = [
        line for line in lines
        if line.ibkr_account.strip().upper() == clean_account
    ]

    exec_schemas = [
        BrokerExecutionLineSchema(
            exec_id=line.exec_id,
            executed_at=line.executed_at,
            ibkr_account=line.ibkr_account,
            symbol=line.symbol,
            sec_type=line.sec_type,
            currency=line.currency,
            exchange=line.exchange,
            con_id=line.con_id,
            side=line.side,
            quantity=line.quantity,
            price=line.price,
            cum_qty=line.cum_qty,
            avg_price=line.avg_price,
            broker_order_id=line.broker_order_id,
            perm_id=line.perm_id,
            client_id=line.client_id,
            commission=line.commission,
            commission_currency=line.commission_currency,
            realized_pnl=line.realized_pnl,
        )
        for line in filtered_lines
    ]

    return BrokerExecutionsResponse(
        ibkr_account=clean_account,
        as_of=datetime.now(UTC),
        timed_out=timed_out,
        window="since_midnight",
        executions=exec_schemas,
    )


@router.get(
    "/trade-book",
    summary="Durable Trade Book (persisted)",
    response_model=TradeBookPaginatedResponse,
)
async def get_trade_book(
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    symbol: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "executed_at",
    dir: Annotated[str, Query()] = "desc",  # noqa: A002
) -> TradeBookPaginatedResponse:
    from datetime import datetime as _dt

    from app.db.repositories.trade_execution_repository import TradeExecutionRepository

    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session2:
        acc = (await session2.execute(select(AccountModel).where(func.upper(AccountModel.ibkr_account) == clean_account))).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")
        # Parse date filters
        df = None
        d_to = None
        if date_from:
            try:
                df = _dt.fromisoformat(date_from)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid date_from")
        if date_to:
            try:
                d_to = _dt.fromisoformat(date_to)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid date_to")
        allowed_sorts = {"executed_at", "symbol", "quantity", "price", "created_at", "last_synced_at"}
        if sort not in allowed_sorts:
            sort = "executed_at"
        direction = dir.lower() if dir.lower() in ("asc", "desc") else "desc"

        repo = TradeExecutionRepository(session2)
        joined, total = await repo.list_paginated_with_order_status(
            account_id=acc.id, page=page, page_size=page_size, symbol=symbol, date_from=df, date_to=d_to, sort=sort, direction=direction
        )
        last_synced = await repo.get_last_synced_at(acc.id)
        exec_schemas = [
            BrokerExecutionLineSchema(
                exec_id=r.exec_id,
                executed_at=r.executed_at.isoformat(),
                ibkr_account=r.ibkr_account,
                symbol=r.symbol,
                sec_type=r.sec_type,
                currency=r.currency,
                exchange=r.exchange,
                con_id=r.con_id or 0,
                side=r.side,
                quantity=float(r.quantity),
                price=float(r.price),
                cum_qty=float(r.cum_qty),
                avg_price=float(r.avg_price),
                broker_order_id=int(r.broker_order_id) if r.broker_order_id and r.broker_order_id.isdigit() else None,
                perm_id=r.perm_id,
                client_id=r.client_id,
                commission=float(r.commission) if r.commission is not None else None,
                commission_currency=r.commission_currency,
                realized_pnl=float(r.realized_pnl) if r.realized_pnl is not None else None,
                order_status=st,
            )
            for r, st in joined
        ]
        sync_state = getattr(request.app.state, "trade_book_sync", None)
        last_sync_obj = sync_state.last_synced_at_for(acc.id) if sync_state else last_synced
        return TradeBookPaginatedResponse(
            ibkr_account=clean_account,
            as_of=datetime.now(UTC),
            last_synced_at=last_sync_obj or last_synced,
            timed_out=False,
            total=total,
            page=page,
            page_size=page_size,
            executions=exec_schemas,
        )


@router.get(
    "/order-book",
    summary="Durable Order Book (persisted)",
    response_model=OrderBookResponse,
)
async def get_order_book(
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    status: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "updated_at",
    dir: Annotated[str, Query()] = "desc",  # noqa: A002
) -> OrderBookResponse:
    from datetime import datetime as _dt

    from app.db.repositories.order_book_repository import OrderBookRepository

    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session2:
        acc = (await session2.execute(select(AccountModel).where(func.upper(AccountModel.ibkr_account) == clean_account))).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")
        df = None
        d_to = None
        if date_from:
            try:
                df = _dt.fromisoformat(date_from)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid date_from")
        if date_to:
            try:
                d_to = _dt.fromisoformat(date_to)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid date_to")
        allowed = {"created_at", "updated_at", "symbol", "status", "quantity"}
        if sort not in allowed:
            sort = "updated_at"
        direction = dir.lower() if dir.lower() in ("asc", "desc") else "desc"
        repo = OrderBookRepository(session2)
        rows, total = await repo.list_paginated(
            account_id=acc.id, page=page, page_size=page_size, status=status, symbol=symbol, date_from=df, date_to=d_to, sort=sort, direction=direction
        )

        def _to_row(r):
            return OrderBookRowSchema(
                internal_order_id=r.internal_order_id or "",
                broker_order_id=r.broker_order_id,
                perm_id=r.perm_id,
                ibkr_account=clean_account,
                symbol=r.symbol,
                sec_type=r.sec_type,
                exchange=r.exchange,
                currency=r.currency,
                ibkr_contract=r.ibkr_contract,
                side=r.buy_sell,
                quantity=float(r.quantity) if r.quantity is not None else 0.0,
                filled=float(r.fill_qty or 0),
                remaining=float(r.remaining_qty) if r.remaining_qty is not None else float(r.quantity or 0) - float(r.fill_qty or 0),
                order_type=r.order_type or "LIMIT",
                limit_price=float(r.limit_price) if r.limit_price is not None else None,
                status=r.status,
                avg_fill_price=float(r.avg_fill_price) if r.avg_fill_price is not None else (float(r.fill_price) if r.fill_price is not None else None),
                last_fill_price=None,
                trade_id=r.trade_id,
                signal_id=str(r.signal_id),
                basket_id=r.basket_id,
                is_compensation=r.is_compensation,
                compensation_of=r.compensation_of_internal_order_id,
                rejection_reason=r.rejection_reason,
                cancel_reason=r.cancel_reason,
                created_at=r.created_at,
                updated_at=r.updated_at,
                filled_at=r.filled_at,
            )

        return OrderBookResponse(
            ibkr_account=clean_account,
            as_of=datetime.now(UTC),
            total=total,
            page=page,
            page_size=page_size,
            orders=[_to_row(r) for r in rows],
        )


@router.get(
    "/order-book/{internal_order_id}",
    summary="Order Book detail",
    response_model=OrderBookRowSchema,
)
async def get_order_book_detail(
    internal_order_id: str,
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
) -> OrderBookRowSchema:
    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)
    from app.db.repositories.order_book_repository import OrderBookRepository
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session2:
        acc = (await session2.execute(select(AccountModel).where(func.upper(AccountModel.ibkr_account) == clean_account))).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")
        repo = OrderBookRepository(session2)
        row = await repo.get_by_internal_id(internal_order_id, account_id=acc.id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Order {internal_order_id} not found")
        return OrderBookRowSchema(
            internal_order_id=row.internal_order_id or "",
            broker_order_id=row.broker_order_id,
            perm_id=row.perm_id,
            ibkr_account=clean_account,
            symbol=row.symbol,
            sec_type=row.sec_type,
            exchange=row.exchange,
            currency=row.currency,
            ibkr_contract=row.ibkr_contract,
            side=row.buy_sell,
            quantity=float(row.quantity) if row.quantity is not None else 0.0,
            filled=float(row.fill_qty or 0),
            remaining=float(row.remaining_qty) if row.remaining_qty is not None else float(row.quantity or 0) - float(row.fill_qty or 0),
            order_type=row.order_type or "LIMIT",
            limit_price=float(row.limit_price) if row.limit_price is not None else None,
            status=row.status,
            avg_fill_price=float(row.avg_fill_price) if row.avg_fill_price is not None else (float(row.fill_price) if row.fill_price is not None else None),
            last_fill_price=None,
            trade_id=row.trade_id,
            signal_id=str(row.signal_id),
            basket_id=row.basket_id,
            is_compensation=row.is_compensation,
            compensation_of=row.compensation_of_internal_order_id,
            rejection_reason=row.rejection_reason,
            cancel_reason=row.cancel_reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
            filled_at=row.filled_at,
        )
