"""Broker execution snapshot API endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import require_authenticated_user
from app.api.routes.config import _check_account_authorization
from app.db.models.user import UserModel
from app.schemas.broker_execution_schemas import (
    BrokerExecutionLineSchema,
    BrokerExecutionsResponse,
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
