"""Dashboard config CRUD for accounts, allocations, and symbol limits."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.config_service import (
    AccountStrategyConfigService,
    AllocationConfigError,
)
from app.api.deps import get_order_manager, require_admin, require_authenticated_user
from app.core.config import get_settings
from app.db.models.account import AccountModel, PerSymbolLimitModel
from app.db.models.strategy import AllocationModel
from app.db.models.user import UserModel
from app.db.session import get_db_session
from app.oms.retry_policy import paper_retry_ports_allowed
from app.schemas.config_schemas import (
    AccountConfigSchema,
    AccountDeleteCheckResponse,
    AccountsConfigResponse,
    AllocationConfigSchema,
    ClosePairResponse,
    CreateAccountRequest,
    CreateAllocationRequest,
    ExecutionSettingsSchema,
    KillSwitchClearResponse,
    KillSwitchStatusResponse,
    MarginSettingsSchema,
    PatchAccountRequest,
    PatchAllocationRequest,
    PatchExecutionSettingsRequest,
    PatchMarginSettingsRequest,
    PutDefaultSymbolLimitRequest,
    PutSymbolLimitRequest,
    SquareOffResponse,
    SymbolLimitSchema,
)
from app.services.kill_switch import KillSwitchService
from app.services.order_manager import OrderManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config", tags=["config"])


def _config_error(exc: AllocationConfigError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _check_account_authorization(
    current_user: UserModel, account_id: int | None = None, ibkr_account: str | None = None
) -> None:
    if current_user.role == "admin":
        return
    if current_user.ibkr_account_id is None:
        raise HTTPException(status_code=403, detail="Forbidden: User has no mapped IBKR account")
    if account_id is not None and account_id != current_user.ibkr_account_id:
        raise HTTPException(status_code=403, detail="Forbidden: Cannot access another account")
    if ibkr_account is not None:
        user_acc_str = current_user.account.ibkr_account if current_user.account else None
        if ibkr_account.strip().upper() != (user_acc_str or "").strip().upper():
            raise HTTPException(status_code=403, detail="Forbidden: Cannot access another account")


@router.get(
    "/accounts",
    response_model=AccountsConfigResponse,
    summary="List accounts with allocations and symbol limits",
)
async def list_accounts_config(
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> AccountsConfigResponse:
    """Return nested config for the settings dashboard."""
    if current_user.role == "user":
        if current_user.ibkr_account_id is None:
            return AccountsConfigResponse(accounts=[])
        accounts = (
            await session.execute(
                select(AccountModel).where(AccountModel.id == current_user.ibkr_account_id)
            )
        ).scalars().all()
    else:
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
    from app.services.kill_switch import is_account_kill_switch_active

    for account in accounts:
        payload.append(
            AccountConfigSchema(
                id=account.id,
                name=account.name,
                ibkr_account=account.ibkr_account,
                total_margin=account.total_margin,
                enabled=account.enabled,
                default_symbol_limit=account.default_symbol_limit,
                kill_switch_active=is_account_kill_switch_active(account.id),
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
    current_user: UserModel = Depends(require_authenticated_user),
) -> AccountConfigSchema:
    _check_account_authorization(current_user, ibkr_account=ibkr_account)
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

    from app.services.kill_switch import is_account_kill_switch_active

    return AccountConfigSchema(
        id=account.id,
        name=account.name,
        ibkr_account=account.ibkr_account,
        total_margin=account.total_margin,
        enabled=account.enabled,
        default_symbol_limit=account.default_symbol_limit,
        kill_switch_active=is_account_kill_switch_active(account.id),
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
    current_user: UserModel = Depends(require_authenticated_user),
) -> SquareOffResponse:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    order_manager: OrderManager | None = getattr(request.app.state, "order_manager", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=503,
            detail="Session factory is unavailable.",
        )

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
    "/accounts/{account_id}/kill-switch/clear",
    response_model=KillSwitchClearResponse,
    summary="Clear an account's kill switch and re-enable new OPEN signals",
)
async def clear_account_kill_switch_endpoint(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> KillSwitchClearResponse:
    """Disarm an account blocked by the emergency kill switch.

    The armed state is durable and survives restarts, so this is the only way
    to resume opening positions on the account. Completing a flatten does not
    disarm on its own -- clearing is always a deliberate operator action.
    """
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=503,
            detail="Session factory is unavailable.",
        )

    from app.services.kill_switch import (
        clear_account_kill_switch,
        is_account_kill_switch_active,
    )

    cleared = await clear_account_kill_switch(
        session_factory, account_id, cleared_by="operator"
    )
    return KillSwitchClearResponse(
        account_id=account_id,
        ibkr_account=account.ibkr_account,
        operations_cleared=cleared,
        kill_switch_active=is_account_kill_switch_active(account_id),
    )


@router.get(
    "/accounts/{account_id}/kill-switch",
    response_model=KillSwitchStatusResponse,
    summary="Report whether an account is blocked from opening new positions",
)
async def get_account_kill_switch_status(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> KillSwitchStatusResponse:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    from app.services.kill_switch import is_account_kill_switch_active

    return KillSwitchStatusResponse(
        account_id=account_id,
        kill_switch_active=is_account_kill_switch_active(account_id),
    )


@router.post(
    "/accounts/{account_id}/positions/{trade_id}/close",
    response_model=ClosePairResponse,
    summary="Close a single selected open position/pair for an account",
)
async def close_selected_pair_endpoint(
    account_id: int,
    trade_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> ClosePairResponse:
    """Close only the selected open pair without affecting other positions or activating the global Kill Switch."""
    _check_account_authorization(current_user, account_id=account_id)
    order_manager: OrderManager | None = getattr(request.app.state, "order_manager", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=503,
            detail="Session factory is unavailable.",
        )

    from app.services.position_close_service import SinglePairCloseService

    close_svc = SinglePairCloseService(
        session_factory=session_factory,
        order_manager=order_manager,
    )
    return await close_svc.close_pair(account_id=account_id, trade_id=trade_id)


@router.post(
    "/accounts",
    response_model=AccountConfigSchema,
    status_code=201,
    summary="Create a new paper trading account",
)
async def create_account(
    body: CreateAccountRequest,
    session: AsyncSession = Depends(get_db_session),
    _admin: UserModel = Depends(require_admin),
) -> AccountConfigSchema:
    svc = AccountStrategyConfigService(session)
    try:
        account = await svc.create_account(
            name=body.name,
            ibkr_account=body.ibkr_account,
            total_margin=body.total_margin,
            enabled=body.enabled,
            default_symbol_limit=body.default_symbol_limit,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info(
        "Config POST account id=%s name=%s ibkr=%s margin=%s enabled=%s default_limit=%s",
        account.id,
        account.name,
        account.ibkr_account,
        account.total_margin,
        account.enabled,
        account.default_symbol_limit,
    )
    return AccountConfigSchema(
        id=account.id,
        name=account.name,
        ibkr_account=account.ibkr_account,
        total_margin=account.total_margin,
        enabled=account.enabled,
        default_symbol_limit=account.default_symbol_limit,
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
    request: Request = None,
    current_user: UserModel = Depends(require_authenticated_user),
) -> AccountConfigSchema:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
    if (
        body.name is None
        and body.ibkr_account is None
        and body.total_margin is None
        and body.enabled is None
        and body.default_symbol_limit is None
    ):
        raise HTTPException(status_code=400, detail="No fields to update.")
    try:
        await svc.update_account(
            account,
            name=body.name,
            ibkr_account=body.ibkr_account,
            total_margin=body.total_margin,
            enabled=body.enabled,
            default_symbol_limit=body.default_symbol_limit,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc

    if request is not None and getattr(request.app.state, "order_manager", None) is not None:
        await request.app.state.order_manager.reload_rms_limits()

    logger.info(
        "Config PATCH account id=%s name=%s ibkr=%s margin=%s enabled=%s default_limit=%s",
        account_id,
        body.name,
        body.ibkr_account,
        body.total_margin,
        body.enabled,
        body.default_symbol_limit,
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
    from app.services.kill_switch import is_account_kill_switch_active

    return AccountConfigSchema(
        id=account.id,
        name=account.name,
        ibkr_account=account.ibkr_account,
        total_margin=account.total_margin,
        enabled=account.enabled,
        default_symbol_limit=account.default_symbol_limit,
        kill_switch_active=is_account_kill_switch_active(account.id),
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
    _admin: UserModel = Depends(require_admin),
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
            pair_max_allocation_pct=body.pair_max_allocation_pct,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info(
        "Config POST allocation account_id=%s strategy=%s pct=%s pair_pct=%s enabled=%s",
        account_id,
        body.strategy_id,
        body.alloc_pct,
        body.pair_max_allocation_pct,
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
    current_user: UserModel = Depends(require_authenticated_user),
) -> AccountDeleteCheckResponse:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
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
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: UserModel = Depends(require_admin),
) -> None:
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
    try:
        await svc.delete_account(account_id)
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc

    order_manager: OrderManager | None = getattr(request.app.state, "order_manager", None)
    if order_manager is not None:
        await order_manager.reload_rms_limits()

    from app.services.kill_switch import _KILL_SWITCH_ACTIVE_ACCOUNTS

    _KILL_SWITCH_ACTIVE_ACCOUNTS.discard(account_id)

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
    current_user: UserModel = Depends(require_authenticated_user),
) -> AllocationConfigSchema:
    svc = AccountStrategyConfigService(session)
    allocation = await svc.get_allocation(allocation_id)
    if allocation is None:
        raise HTTPException(status_code=404, detail=f"Allocation {allocation_id} not found.")
    _check_account_authorization(current_user, account_id=allocation.account_id)
    if (
        body.alloc_pct is None
        and body.enabled is None
        and body.max_open_positions is None
        and body.pair_max_allocation_pct is None
    ):
        raise HTTPException(status_code=400, detail="No fields to update.")
    try:
        await svc.update_allocation(
            allocation,
            alloc_pct=body.alloc_pct,
            enabled=body.enabled,
            max_open_positions=body.max_open_positions,
            pair_max_allocation_pct=body.pair_max_allocation_pct,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    logger.info(
        "Config PATCH allocation id=%s pct=%s enabled=%s cap=%s pair_pct=%s",
        allocation_id,
        body.alloc_pct,
        body.enabled,
        body.max_open_positions,
        body.pair_max_allocation_pct,
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
    current_user: UserModel = Depends(require_authenticated_user),
) -> SymbolLimitSchema:
    _check_account_authorization(current_user, account_id=account_id)
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


@router.put(
    "/accounts/{account_id}/default-symbol-limit",
    response_model=AccountConfigSchema,
    summary="Update default symbol money limit for account",
)
async def put_default_symbol_limit(
    account_id: int,
    body: PutDefaultSymbolLimitRequest,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
    current_user: UserModel = Depends(require_authenticated_user),
) -> AccountConfigSchema:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
    try:
        await svc.update_account(account, default_symbol_limit=body.default_symbol_limit)
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc

    await order_manager.reload_rms_limits()
    logger.info(
        "Config PUT default symbol limit account=%s limit=%s",
        account_id,
        body.default_symbol_limit,
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
    from app.services.kill_switch import is_account_kill_switch_active

    return AccountConfigSchema(
        id=account.id,
        name=account.name,
        ibkr_account=account.ibkr_account,
        total_margin=account.total_margin,
        enabled=account.enabled,
        default_symbol_limit=account.default_symbol_limit,
        kill_switch_active=is_account_kill_switch_active(account.id),
        allocations=[AllocationConfigSchema.model_validate(a) for a in allocations],
        symbol_limits=[SymbolLimitSchema.model_validate(l) for l in limits],
    )


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
    current_user: UserModel = Depends(require_authenticated_user),
) -> None:
    _check_account_authorization(current_user, account_id=account_id)
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
    _user: UserModel = Depends(require_authenticated_user),
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
    _admin: UserModel = Depends(require_admin),
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


def _margin_schema(row) -> MarginSettingsSchema:
    return MarginSettingsSchema(
        check_enabled=row.check_enabled,
        gate_basis=row.gate_basis,
        min_free_buffer=row.min_free_buffer,
        min_free_pct_of_netliq=row.min_free_pct_of_netliq,
        comfort_ratio=row.comfort_ratio,
        confirm_borderline=row.confirm_borderline,
        enforce_look_ahead=row.enforce_look_ahead,
        reject_on_stale_snapshot=row.reject_on_stale_snapshot,
        default_rate=row.default_rate,
        rate_safety_multiplier=row.rate_safety_multiplier,
    )


@router.get(
    "/margin",
    response_model=MarginSettingsSchema,
    summary="Margin-gate operator policy",
)
async def get_margin_settings(
    session: AsyncSession = Depends(get_db_session),
    _user: UserModel = Depends(require_authenticated_user),
) -> MarginSettingsSchema:
    svc = AccountStrategyConfigService(session)
    row = await svc.get_or_create_margin_settings()
    await session.commit()
    return _margin_schema(row)


@router.patch(
    "/margin",
    response_model=MarginSettingsSchema,
    summary="Update margin-gate operator policy",
)
async def patch_margin_settings(
    body: PatchMarginSettingsRequest,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
    _admin: UserModel = Depends(require_admin),
) -> MarginSettingsSchema:
    if (
        body.check_enabled is None
        and body.gate_basis is None
        and body.min_free_buffer is None
        and body.min_free_pct_of_netliq is None
        and body.comfort_ratio is None
        and body.confirm_borderline is None
        and body.enforce_look_ahead is None
        and body.reject_on_stale_snapshot is None
        and body.default_rate is None
        and body.rate_safety_multiplier is None
    ):
        raise HTTPException(status_code=400, detail="No fields to update.")
    svc = AccountStrategyConfigService(session)
    try:
        row = await svc.update_margin_settings(
            check_enabled=body.check_enabled,
            gate_basis=body.gate_basis,
            min_free_buffer=body.min_free_buffer,
            min_free_pct_of_netliq=body.min_free_pct_of_netliq,
            comfort_ratio=body.comfort_ratio,
            confirm_borderline=body.confirm_borderline,
            enforce_look_ahead=body.enforce_look_ahead,
            reject_on_stale_snapshot=body.reject_on_stale_snapshot,
            default_rate=body.default_rate,
            rate_safety_multiplier=body.rate_safety_multiplier,
        )
        await session.commit()
    except AllocationConfigError as exc:
        await session.rollback()
        raise _config_error(exc) from exc
    await order_manager.reload_margin_settings()
    logger.info(
        "Config PATCH margin check_enabled=%s basis=%s comfort=%s",
        row.check_enabled,
        row.gate_basis,
        row.comfort_ratio,
    )
    return _margin_schema(row)

