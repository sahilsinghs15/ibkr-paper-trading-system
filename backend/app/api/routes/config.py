"""Dashboard config CRUD for accounts, allocations, and symbol limits."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.config_service import (
    AccountStrategyConfigService,
    AllocationConfigError,
)
from app.api.deps import get_order_manager
from app.db.models.account import AccountModel, PerSymbolLimitModel
from app.db.models.strategy import AllocationModel
from app.db.session import get_db_session
from app.schemas.config_schemas import (
    AccountConfigSchema,
    AccountsConfigResponse,
    AllocationConfigSchema,
    PatchAccountRequest,
    PatchAllocationRequest,
    PutSymbolLimitRequest,
    SymbolLimitSchema,
)
from app.services.order_manager import OrderManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config", tags=["config"])


def _config_error(exc: AllocationConfigError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get(
    "/accounts",
    response_model=AccountsConfigResponse,
    summary="List accounts with allocations and symbol limits",
)
async def list_accounts_config(
    session: AsyncSession = Depends(get_db_session),
) -> AccountsConfigResponse:
    """Return nested config for the settings dashboard."""
    accounts = (await session.execute(select(AccountModel).order_by(AccountModel.id))).scalars().all()
    allocations = (
        await session.execute(select(AllocationModel).order_by(AllocationModel.account_id))
    ).scalars().all()
    limits = (
        await session.execute(
            select(PerSymbolLimitModel).order_by(
                PerSymbolLimitModel.account_id, PerSymbolLimitModel.symbol
            )
        )
    ).scalars().all()

    allocs_by_account: dict[int, list[AllocationModel]] = {}
    for alloc in allocations:
        allocs_by_account.setdefault(alloc.account_id, []).append(alloc)

    limits_by_account: dict[int, list[PerSymbolLimitModel]] = {}
    for limit in limits:
        limits_by_account.setdefault(limit.account_id, []).append(limit)

    payload: list[AccountConfigSchema] = []
    for account in accounts:
        payload.append(
            AccountConfigSchema(
                id=account.id,
                name=account.name,
                ibkr_account=account.ibkr_account,
                total_margin=account.total_margin,
                enabled=account.enabled,
                allocations=[
                    AllocationConfigSchema.model_validate(a)
                    for a in allocs_by_account.get(account.id, [])
                ],
                symbol_limits=[
                    SymbolLimitSchema.model_validate(l)
                    for l in limits_by_account.get(account.id, [])
                ],
            )
        )
    return AccountsConfigResponse(accounts=payload)


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountConfigSchema,
    summary="Update account margin or enabled flag",
)
async def patch_account(
    account_id: int,
    body: PatchAccountRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AccountConfigSchema:
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
    if body.total_margin is None and body.enabled is None:
        raise HTTPException(status_code=400, detail="No fields to update.")
    try:
        await svc.update_account(
            account,
            total_margin=body.total_margin,
            enabled=body.enabled,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info(
        "Config PATCH account id=%s margin=%s enabled=%s",
        account_id,
        body.total_margin,
        body.enabled,
    )
    allocations = (
        await session.execute(
            select(AllocationModel).where(AllocationModel.account_id == account_id)
        )
    ).scalars().all()
    limits = (
        await session.execute(
            select(PerSymbolLimitModel).where(PerSymbolLimitModel.account_id == account_id)
        )
    ).scalars().all()
    return AccountConfigSchema(
        id=account.id,
        name=account.name,
        ibkr_account=account.ibkr_account,
        total_margin=account.total_margin,
        enabled=account.enabled,
        allocations=[AllocationConfigSchema.model_validate(a) for a in allocations],
        symbol_limits=[SymbolLimitSchema.model_validate(l) for l in limits],
    )


@router.patch(
    "/allocations/{allocation_id}",
    response_model=AllocationConfigSchema,
    summary="Update allocation pct, enabled, or position cap",
)
async def patch_allocation(
    allocation_id: int,
    body: PatchAllocationRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AllocationConfigSchema:
    svc = AccountStrategyConfigService(session)
    allocation = await svc.get_allocation(allocation_id)
    if allocation is None:
        raise HTTPException(status_code=404, detail=f"Allocation {allocation_id} not found.")
    if (
        body.alloc_pct is None
        and body.enabled is None
        and body.max_open_positions is None
    ):
        raise HTTPException(status_code=400, detail="No fields to update.")
    try:
        await svc.update_allocation(
            allocation,
            alloc_pct=body.alloc_pct,
            enabled=body.enabled,
            max_open_positions=body.max_open_positions,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info(
        "Config PATCH allocation id=%s pct=%s enabled=%s cap=%s",
        allocation_id,
        body.alloc_pct,
        body.enabled,
        body.max_open_positions,
    )
    return AllocationConfigSchema.model_validate(allocation)


@router.put(
    "/accounts/{account_id}/symbol-limits/{symbol}",
    response_model=SymbolLimitSchema,
    summary="Upsert per-symbol money limit",
)
async def put_symbol_limit(
    account_id: int,
    symbol: str,
    body: PutSymbolLimitRequest,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
) -> SymbolLimitSchema:
    svc = AccountStrategyConfigService(session)
    try:
        row = await svc.upsert_symbol_limit(
            account_id=account_id,
            symbol=symbol,
            money_limit=body.money_limit,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    await order_manager.reload_rms_limits()
    logger.info(
        "Config PUT symbol limit account=%s symbol=%s limit=%s",
        account_id,
        symbol,
        body.money_limit,
    )
    return SymbolLimitSchema.model_validate(row)


@router.delete(
    "/accounts/{account_id}/symbol-limits/{symbol}",
    status_code=204,
    summary="Remove per-symbol money limit",
)
async def delete_symbol_limit(
    account_id: int,
    symbol: str,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
) -> None:
    svc = AccountStrategyConfigService(session)
    deleted = await svc.delete_symbol_limit(account_id=account_id, symbol=symbol)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Symbol limit {symbol!r} not found for account {account_id}.",
        )
    await session.commit()
    await order_manager.reload_rms_limits()
    logger.info("Config DELETE symbol limit account=%s symbol=%s", account_id, symbol)
