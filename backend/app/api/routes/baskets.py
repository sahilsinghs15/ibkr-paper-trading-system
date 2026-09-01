"""Read-only critical basket incidents for the positions dashboard."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.repositories.basket_repository import BasketRepository
from app.db.repositories.order_repository import OrderRepository
from app.db.session import get_db_session
from app.schemas.critical_basket_schemas import (
    CriticalBasketLegRow,
    CriticalBasketRow,
    CriticalBasketsResponse,
)
from app.services.critical_recovery import parse_ibkr_contract

router = APIRouter(prefix="/baskets", tags=["baskets"])


async def collect_critical_baskets(
    session: AsyncSession,
    *,
    ibkr_account: str,
) -> CriticalBasketsResponse:
    clean = ibkr_account.strip().upper()
    account = (
        await session.execute(
            select(AccountModel).where(func.upper(AccountModel.ibkr_account) == clean)
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account '{ibkr_account}' not found.")

    rows = await BasketRepository(session).list_critical_for_ibkr_account(account.ibkr_account)
    incidents: list[CriticalBasketRow] = []
    order_repo = OrderRepository(session)
    for row in rows:
        orders = await order_repo.list_by_basket_id(row.id)
        legs: list[CriticalBasketLegRow] = []
        for order in orders:
            if order.is_compensation:
                continue
            symbol, sec_type, _ex, _cur, con_id = parse_ibkr_contract(order.ibkr_contract)
            legs.append(
                CriticalBasketLegRow(
                    leg=order.leg,
                    symbol=order.symbol,
                    sec_type=sec_type,
                    con_id=con_id,
                    intended_qty=float(order.quantity),
                    filled_qty=float(order.fill_qty or 0),
                    status=order.status,
                )
            )
        incidents.append(
            CriticalBasketRow(
                basket_id=row.id,
                account_id=row.account_id,
                ibkr_account=account.ibkr_account,
                strategy_id=row.strategy_id,
                trade_id=row.trade_id,
                action=row.action,
                state=row.state,
                recovery_status=row.recovery_status,
                recovery_detail=row.recovery_detail,
                recovered_at=row.recovered_at,
                intended_leg_count=row.intended_leg_count,
                legs=legs,
                updated_at=row.updated_at,
            )
        )
    return CriticalBasketsResponse(ibkr_account=account.ibkr_account, incidents=incidents)


@router.get(
    "/critical",
    summary="List CRITICAL basket incidents for an IBKR account",
    description=(
        "Returns baskets in CRITICAL state (including in-flight RECOVERING recovery). "
        "An empty list means no OPEN block is active for that account's strategies."
    ),
    response_model=CriticalBasketsResponse,
)
async def list_critical_baskets(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    ibkr_account: Annotated[
        str,
        Query(description="IBKR account id (e.g. DUR919062)"),
    ],
) -> CriticalBasketsResponse:
    if not ibkr_account.strip():
        raise HTTPException(status_code=400, detail="ibkr_account is required.")
    return await collect_critical_baskets(db, ibkr_account=ibkr_account)
