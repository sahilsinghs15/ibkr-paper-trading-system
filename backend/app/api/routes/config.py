"""Dashboard config CRUD for accounts, allocations, and symbol limits."""

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.config_service import (
    AccountStrategyConfigService,
    AllocationConfigError,
)
from app.api.deps import get_order_manager
from app.core.config import get_settings
from app.services.kill_switch import KillSwitchService
from app.db.models.account import AccountModel, PerSymbolLimitModel
from app.db.models.strategy import AllocationModel
from app.db.session import get_db_session
from app.oms.retry_policy import paper_retry_ports_allowed
from app.schemas.config_schemas import (
    AccountConfigSchema,
    AccountDeleteCheckResponse,
    AccountsConfigResponse,
    AllocationConfigSchema,
    CreateAccountRequest,
    CreateAllocationRequest,
    ExecutionSettingsSchema,
    PatchAccountRequest,
    PatchAllocationRequest,
    PatchExecutionSettingsRequest,
    PutSymbolLimitRequest,
    SquareOffResponse,
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


@router.get(
    "/accounts/by-identifier/{ibkr_account}",
    response_model=AccountConfigSchema,
    summary="Get account config by IBKR account identifier",
)
async def get_account_by_identifier(
    ibkr_account: str,
    session: AsyncSession = Depends(get_db_session),
) -> AccountConfigSchema:
    clean_ibkr = ibkr_account.strip().upper()
    account = (
        await session.execute(
            select(AccountModel).where(func.upper(AccountModel.ibkr_account) == clean_ibkr)
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=404, detail=f"Account '{ibkr_account}' not found."
        )

    allocations = (
        await session.execute(
            select(AllocationModel).where(AllocationModel.account_id == account.id)
        )
    ).scalars().all()
    limits = (
        await session.execute(
            select(PerSymbolLimitModel).where(PerSymbolLimitModel.account_id == account.id)
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


@router.post(
    "/accounts/{account_id}/square-off",
    response_model=SquareOffResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Emergency Kill Switch: Square off all open positions for account",
)
async def square_off_account_positions(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SquareOffResponse:
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    order_manager: OrderManager | None = getattr(request.app.state, "order_manager", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        from app.db.session import AsyncSessionLocal
        session_factory = AsyncSessionLocal

    kill_switch_svc = KillSwitchService(
        session_factory=session_factory,
        order_manager=order_manager,
    )

    op, created_new = await kill_switch_svc.initiate_square_off(
        account_id=account_id, requested_by="operator"
    )
    if created_new:
        await kill_switch_svc.execute_flatten_operation_background(op.operation_id)

    return SquareOffResponse(
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        squared_off_count=op.initial_position_count,
        trade_ids=[],
        operation_id=str(op.operation_id),
        status=op.status,
    )


@router.post(
    "/accounts",
    response_model=AccountConfigSchema,
    status_code=201,
    summary="Create a new paper trading account",
)
async def create_account(
    body: CreateAccountRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AccountConfigSchema:
    svc = AccountStrategyConfigService(session)
    try:
        account = await svc.create_account(
            name=body.name,
            ibkr_account=body.ibkr_account,
            total_margin=body.total_margin,
            enabled=body.enabled,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info(
        "Config POST account id=%s name=%s ibkr=%s margin=%s enabled=%s",
        account.id,
        account.name,
        account.ibkr_account,
        account.total_margin,
        account.enabled,
    )
    return AccountConfigSchema(
        id=account.id,
        name=account.name,
        ibkr_account=account.ibkr_account,
        total_margin=account.total_margin,
        enabled=account.enabled,
        allocations=[],
        symbol_limits=[],
    )


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountConfigSchema,
    summary="Update account name, IBKR identifier, margin or enabled flag",
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
    if (
        body.name is None
        and body.ibkr_account is None
        and body.total_margin is None
        and body.enabled is None
    ):
        raise HTTPException(status_code=400, detail="No fields to update.")
    try:
        await svc.update_account(
            account,
            name=body.name,
            ibkr_account=body.ibkr_account,
            total_margin=body.total_margin,
            enabled=body.enabled,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info(
        "Config PATCH account id=%s name=%s ibkr=%s margin=%s enabled=%s",
        account_id,
        body.name,
        body.ibkr_account,
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


@router.post(
    "/accounts/{account_id}/allocations",
    response_model=AllocationConfigSchema,
    status_code=201,
    summary="Assign strategy allocation to account",
)
async def create_account_allocation(
    account_id: int,
    body: CreateAllocationRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AllocationConfigSchema:
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
    try:
        allocation = await svc.create_allocation(
            account=account,
            strategy_id=body.strategy_id,
            alloc_pct=body.alloc_pct,
            target=body.target,
            stop=body.stop,
            time_limit=body.time_limit,
            enabled=body.enabled,
            max_open_positions=body.max_open_positions,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info(
        "Config POST allocation account_id=%s strategy=%s pct=%s enabled=%s",
        account_id,
        body.strategy_id,
        body.alloc_pct,
        body.enabled,
    )
    return AllocationConfigSchema.model_validate(allocation)


@router.get(
    "/accounts/{account_id}/deletable",
    response_model=AccountDeleteCheckResponse,
    summary="Check if an account can be safely deleted",
)
async def check_account_deletable_api(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> AccountDeleteCheckResponse:
    svc = AccountStrategyConfigService(session)
    can_del, reason = await svc.check_account_deletable(account_id)
    history = await svc.has_trading_history(account_id) if not can_del else False
    return AccountDeleteCheckResponse(
        can_delete=can_del,
        reason=reason,
        has_history=history,
    )


@router.delete(
    "/accounts/{account_id}",
    status_code=204,
    summary="Safely delete account without trading history",
)
async def delete_account_api(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    svc = AccountStrategyConfigService(session)
    try:
        await svc.delete_account(account_id)
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info("Config DELETE account id=%s", account_id)


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


def _execution_schema(row, *, paper_active: bool) -> ExecutionSettingsSchema:
    return ExecutionSettingsSchema(
        enabled=row.enabled,
        square_off_after_sec=row.square_off_after_sec,
        max_retries=row.max_retries,
        retry_interval_sec=row.retry_interval_sec,
        retry_window_sec=row.retry_window_sec,
        paper_retries_active=paper_active and row.enabled,
    )


@router.get(
    "/execution",
    response_model=ExecutionSettingsSchema,
    summary="Paper auto square-off and retry settings",
)
async def get_execution_settings(
    session: AsyncSession = Depends(get_db_session),
) -> ExecutionSettingsSchema:
    svc = AccountStrategyConfigService(session)
    row = await svc.get_or_create_execution_settings()
    await session.commit()
    paper = paper_retry_ports_allowed(get_settings().ibkr_port)
    return _execution_schema(row, paper_active=paper)


@router.patch(
    "/execution",
    response_model=ExecutionSettingsSchema,
    summary="Update paper auto square-off and retry settings",
)
async def patch_execution_settings(
    body: PatchExecutionSettingsRequest,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
) -> ExecutionSettingsSchema:
    if (
        body.enabled is None
        and body.square_off_after_sec is None
        and body.max_retries is None
        and body.retry_interval_sec is None
        and body.retry_window_sec is None
    ):
        raise HTTPException(status_code=400, detail="No fields to update.")
    svc = AccountStrategyConfigService(session)
    try:
        row = await svc.update_execution_settings(
            enabled=body.enabled,
            square_off_after_sec=body.square_off_after_sec,
            max_retries=body.max_retries,
            retry_interval_sec=body.retry_interval_sec,
            retry_window_sec=body.retry_window_sec,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    await order_manager.reload_execution_policy()
    logger.info(
        "Config PATCH execution enabled=%s timeout=%s retries=%s interval=%s window=%s",
        row.enabled,
        row.square_off_after_sec,
        row.max_retries,
        row.retry_interval_sec,
        row.retry_window_sec,
    )
    paper = paper_retry_ports_allowed(get_settings().ibkr_port)
    return _execution_schema(row, paper_active=paper)

