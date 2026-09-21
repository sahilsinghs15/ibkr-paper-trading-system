"""Dashboard config CRUD for accounts, allocations, and symbol limits."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.config_service import (
    AccountStrategyConfigService,
    AllocationConfigError,
)
from app.api.deps import get_order_manager, require_admin, require_authenticated_user
from app.audit.context import actor_for_user
from app.audit.recorder import (
    audit_entry,
    changed_fields,
    get_audit_recorder,
    model_snapshot,
)
from app.audit.taxonomy import AuditAction, AuditResult
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
    PatchPositionExitsRequest,
    PositionExitsSchema,
    PutDefaultSymbolLimitRequest,
    PutSymbolLimitRequest,
    SquareOffResponse,
    SymbolLimitSchema,
    TradingPauseResponse,
)
from app.services.kill_switch import KillSwitchService
from app.services.order_manager import OrderManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config", tags=["config"])


def _config_error(exc: AllocationConfigError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _account_risk_fields(account: AccountModel) -> dict:
    return {
        "daily_target": getattr(account, "daily_target", None),
        "daily_stop": getattr(account, "daily_stop", None),
        "daily_target_unit": getattr(account, "daily_target_unit", None) or "ABSOLUTE",
        "daily_stop_unit": getattr(account, "daily_stop_unit", None) or "ABSOLUTE",
        "account_risk_enabled": bool(getattr(account, "account_risk_enabled", False)),
        "loss_threshold": getattr(account, "loss_threshold", None),
        "cancel_exposure": bool(getattr(account, "cancel_exposure", False)),
    }


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
                trading_paused=account.trading_paused,
                paused_at=account.paused_at,
                paused_by=account.paused_by,
                **_account_risk_fields(account),
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
        trading_paused=account.trading_paused,
        paused_at=account.paused_at,
        paused_by=account.paused_by,
        **_account_risk_fields(account),
        allocations=[AllocationConfigSchema.model_validate(a) for a in allocations],
        symbol_limits=[SymbolLimitSchema.model_validate(l) for l in limits],
    )


@router.post(
    "/accounts/{account_id}/square-off",
    response_model=SquareOffResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Emergency Kill Switch: Square off open engine positions for account",
)
async def square_off_account_positions(
    account_id: int,
    request: Request,
    scope: str | None = Query(
        "engine", description="Flatten scope: 'engine', 'manual' or 'account'"
    ),
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> SquareOffResponse:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    if scope == "account":
        return await square_off_entire_ibkr_account(account_id, request, session, current_user)
    if scope == "manual":
        return await square_off_manual_positions(account_id, request, session, current_user)

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

    from app.services.kill_switch import is_account_kill_switch_active

    entry = audit_entry(
        request,
        action=AuditAction.KILL_SWITCH_ENGINE_FLATTEN,
        actor=actor_for_user(request, current_user),
        summary=f"Kill Switch: flatten engine/signal positions on {account.ibkr_account}",
        account_id=account_id,
        ibkr_account=account.ibkr_account,
        target_type="ACCOUNT",
        target_id=account.ibkr_account,
        parameters={"account_id": account_id, "scope": "engine"},
        before_state={"kill_switch_active": is_account_kill_switch_active(account_id)},
    )
    # Emergency risk reduction must never be blocked by an audit outage.
    async with get_audit_recorder(request).operation(
        entry, required=False, success_result=AuditResult.ACCEPTED
    ) as audit_op:
        op, created_new = await kill_switch_svc.initiate_square_off(
            account_id=account_id, requested_by="operator"
        )
        if created_new:
            await kill_switch_svc.execute_flatten_operation_background(op.operation_id)
        audit_op.add_related(operation_id=str(op.operation_id))
        audit_op.set_outcome(
            AuditResult.ACCEPTED,
            after={
                "operation_id": str(op.operation_id),
                "operation_status": op.status,
                "initial_position_count": op.initial_position_count,
                "new_operation_started": created_new,
                "kill_switch_active": is_account_kill_switch_active(account_id),
            },
            reason=None if created_new else "Joined an already-running flatten operation.",
        )

    from app.db.repositories.event_repository import EventRepository

    await EventRepository(session).append(
        process="kill_switch",
        kind="ENGINE_POSITION_FLATTEN",
        detail={
            "account_id": account_id,
            "ibkr_account": account.ibkr_account,
            "operation_id": str(op.operation_id),
            "requested_by": "operator",
            "initial_position_count": op.initial_position_count,
            "scope": "ENGINE_POSITION_FLATTEN",
        },
        idempotency_key=f"engine_position_flatten:{op.operation_id}",
    )
    await session.commit()

    return SquareOffResponse(
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        squared_off_count=op.initial_position_count,
        trade_ids=[],
        operation_id=str(op.operation_id),
        status=op.status,
        scope="ENGINE_POSITION_FLATTEN",
    )


@router.post(
    "/accounts/{account_id}/square-off-manual",
    response_model=SquareOffResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Emergency Kill Switch: Flatten manual positions only for account",
)
async def square_off_manual_positions(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> SquareOffResponse:
    """Flatten only manual positions for the specified account without touching engine/signal positions."""
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

    from app.services.kill_switch import is_account_kill_switch_active

    entry = audit_entry(
        request,
        action=AuditAction.KILL_MANUAL_FLATTEN,
        actor=actor_for_user(request, current_user),
        summary=f"Kill Manual: flatten manual positions on {account.ibkr_account}",
        account_id=account_id,
        ibkr_account=account.ibkr_account,
        target_type="ACCOUNT",
        target_id=account.ibkr_account,
        parameters={"account_id": account_id, "scope": "manual"},
        before_state={"kill_switch_active": is_account_kill_switch_active(account_id)},
    )
    async with get_audit_recorder(request).operation(
        entry, required=False, success_result=AuditResult.ACCEPTED
    ) as audit_op:
        op, created_new = await kill_switch_svc.initiate_manual_square_off(
            account_id=account_id, requested_by="operator"
        )
        if created_new:
            await kill_switch_svc.execute_manual_flatten_operation_background(op.operation_id)
        audit_op.add_related(operation_id=str(op.operation_id))
        audit_op.set_outcome(
            AuditResult.ACCEPTED,
            after={
                "operation_id": str(op.operation_id),
                "operation_status": op.status,
                "initial_position_count": op.initial_position_count,
                "new_operation_started": created_new,
            },
            reason=None if created_new else "Joined an already-running flatten operation.",
        )

    from app.db.repositories.event_repository import EventRepository

    await EventRepository(session).append(
        process="kill_switch",
        kind="MANUAL_POSITION_FLATTEN",
        detail={
            "account_id": account_id,
            "ibkr_account": account.ibkr_account,
            "operation_id": str(op.operation_id),
            "requested_by": "operator",
            "initial_position_count": op.initial_position_count,
            "scope": "MANUAL_POSITION_FLATTEN",
        },
        idempotency_key=f"manual_position_flatten:{op.operation_id}",
    )
    await session.commit()

    return SquareOffResponse(
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        squared_off_count=op.initial_position_count,
        trade_ids=[],
        operation_id=str(op.operation_id),
        status=op.status,
        scope="MANUAL_POSITION_FLATTEN",
    )


@router.post(
    "/accounts/{account_id}/square-off-account",
    response_model=SquareOffResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Emergency Kill Switch: Flatten all broker positions for account at IBKR",
)
async def square_off_entire_ibkr_account(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> SquareOffResponse:
    """Flatten all positions held in the IBKR account via existing flatten_gateway_positions path.

    Also arms the account kill switch so new OPEN signals are immediately blocked.
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

    order_manager: OrderManager | None = getattr(request.app.state, "order_manager", None)
    kill_switch_svc = KillSwitchService(
        session_factory=session_factory,
        order_manager=order_manager,
    )

    from app.services.kill_switch import is_account_kill_switch_active

    entry = audit_entry(
        request,
        action=AuditAction.COMPLETE_ACCOUNT_FLATTEN,
        actor=actor_for_user(request, current_user),
        summary=f"Complete Flatten: flatten ALL broker positions on {account.ibkr_account}",
        account_id=account_id,
        ibkr_account=account.ibkr_account,
        target_type="ACCOUNT",
        target_id=account.ibkr_account,
        parameters={"account_id": account_id, "scope": "account", "sec_type": "CFD"},
        before_state={"kill_switch_active": is_account_kill_switch_active(account_id)},
    )
    async with get_audit_recorder(request).operation(entry, required=False) as audit_op:
        # Arm account kill switch so OPEN signals stay blocked
        op, _ = await kill_switch_svc.arm_account_kill_switch_only(
            account_id=account_id, requested_by="operator_account_flatten"
        )
        audit_op.add_related(operation_id=str(op.operation_id))

        from app.db.repositories.event_repository import EventRepository

        await EventRepository(session).append(
            process="kill_switch",
            kind="ACCOUNT_POSITION_FLATTEN",
            detail={
                "account_id": account_id,
                "ibkr_account": account.ibkr_account,
                "operation_id": str(op.operation_id),
                "requested_by": "operator",
                "scope": "ACCOUNT_POSITION_FLATTEN",
            },
            idempotency_key=f"account_position_flatten:{op.operation_id}",
        )
        await session.commit()

        import asyncio

        from scripts.oms.flatten_gateway_positions import run_flatten_gateway_positions

        res = await asyncio.to_thread(
            run_flatten_gateway_positions,
            account=account.ibkr_account,
            sec_type="CFD",
            apply=True,
        )

        # If broker flatten succeeded, also close ledger ghosts for this account (engine+manual)
        # This is the correct accounting path for scope=account – operator explicitly authorized
        # to flatten entire IBKR account, so ledger must converge to broker-flat.
        ledger_closed = None
        if res.get("success"):
            try:
                ledger_closed = await kill_switch_svc.close_all_ledger_after_account_flatten(account_id, op.operation_id)
                logger.info("Account flatten ledger close: account_id=%s closed=%s", account_id, ledger_closed)
            except Exception:
                logger.exception("Account flatten ledger close failed account_id=%s", account_id)

        # Re-read operation status after potential ledger close
        from app.db.models.kill_switch import KillSwitchOperationModel as _KSO

        async with session_factory() as _s:
            _op_row = await _s.get(_KSO, op.operation_id)
            _status = _op_row.status if _op_row else ("COMPLETE" if res.get("success") else "UNRESOLVED")

        submitted = res.get("submitted", 0)
        if res.get("success"):
            flatten_result = AuditResult.SUCCEEDED
        elif submitted:
            flatten_result = AuditResult.PARTIAL
        else:
            flatten_result = AuditResult.FAILED
        audit_op.set_outcome(
            flatten_result,
            reason=res.get("error"),
            after={
                "operation_id": str(op.operation_id),
                "operation_status": _status,
                "broker_positions_found": res.get("positions_found"),
                "broker_orders_submitted": submitted,
                "broker_orders_filled": res.get("filled"),
                "broker_orders_rejected": res.get("rejected"),
                "broker_orders_pending": res.get("pending"),
                "broker_flatten_success": bool(res.get("success")),
                "ledger_rows_closed": ledger_closed,
                "kill_switch_active": is_account_kill_switch_active(account_id),
            },
        )

    return SquareOffResponse(
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        squared_off_count=res.get("submitted", 0),
        trade_ids=[],
        operation_id=str(op.operation_id),
        status=_status,
        scope="ACCOUNT_POSITION_FLATTEN",
        error=res.get("error"),
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

    from app.db.repositories.event_repository import EventRepository
    from app.services.kill_switch import (
        clear_account_kill_switch,
        is_account_kill_switch_active,
    )

    orchestrator = getattr(request.app.state, "notification_orchestrator", None)
    entry = audit_entry(
        request,
        action=AuditAction.KILL_SWITCH_CLEARED,
        actor=actor_for_user(request, current_user),
        summary=f"Kill Switch cleared (Start Again) on {account.ibkr_account}",
        account_id=account_id,
        ibkr_account=account.ibkr_account,
        target_type="ACCOUNT",
        target_id=account.ibkr_account,
        parameters={"account_id": account_id},
        before_state={"kill_switch_active": is_account_kill_switch_active(account_id)},
    )
    async with get_audit_recorder(request).operation(entry) as audit_op:
        cleared = await clear_account_kill_switch(
            session_factory,
            account_id,
            cleared_by=getattr(current_user, "email", None) or "operator",
            notification_orchestrator=orchestrator,
        )
        audit_op.set_outcome(
            AuditResult.SUCCEEDED,
            after={
                "operations_cleared": cleared,
                "kill_switch_active": is_account_kill_switch_active(account_id),
            },
        )
    await EventRepository(session).append(
        process="kill_switch",
        kind="KILL_SWITCH_CLEARED",
        detail={
            "account_id": account_id,
            "ibkr_account": account.ibkr_account,
            "operations_cleared": cleared,
            "cleared_by": getattr(current_user, "email", "operator"),
            "scope": "KILL_SWITCH_CLEARED",
            "source": "frontend",
        },
    )
    await session.commit()

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

    from app.services.kill_switch import (
        get_armed_kill_switch_operation,
        is_account_kill_switch_active,
    )

    active = is_account_kill_switch_active(account_id)
    requested_by: str | None = None
    op_status: str | None = None
    if active:
        armed_op = await get_armed_kill_switch_operation(session, account_id)
        if armed_op is not None:
            requested_by = armed_op.requested_by
            op_status = armed_op.status

    return KillSwitchStatusResponse(
        account_id=account_id,
        kill_switch_active=active,
        requested_by=requested_by,
        status=op_status,
    )


@router.get(
    "/accounts/{account_id}/trading-pause",
    response_model=TradingPauseResponse,
    summary="Report whether an account is paused for new OPEN orders",
)
async def get_account_trading_pause_status(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> TradingPauseResponse:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    from app.services.trading_pause import is_account_trading_paused

    return TradingPauseResponse(
        account_id=account_id,
        ibkr_account=account.ibkr_account,
        trading_paused=is_account_trading_paused(account_id) or account.trading_paused,
        paused_at=account.paused_at,
        paused_by=account.paused_by,
    )


@router.post(
    "/accounts/{account_id}/trading-pause",
    response_model=TradingPauseResponse,
    summary="Pause new opening trading signals for an account",
)
async def pause_account_trading_endpoint(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> TradingPauseResponse:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        from app.db.session import AsyncSessionLocal

        session_factory = AsyncSessionLocal

    from app.services.trading_pause import TradingPauseService

    pause_svc = TradingPauseService(session_factory)
    # UserModel has no ``username``; attribute the pause to the authenticated e-mail.
    actor = getattr(current_user, "email", None) or "operator"
    entry = audit_entry(
        request,
        action=AuditAction.TRADING_PAUSED,
        actor=actor_for_user(request, current_user),
        summary=f"Trading paused (new opens blocked) on {account.ibkr_account}",
        account_id=account_id,
        ibkr_account=account.ibkr_account,
        target_type="ACCOUNT",
        target_id=account.ibkr_account,
        parameters={"account_id": account_id},
        before_state={"trading_paused": account.trading_paused, "paused_by": account.paused_by},
    )
    async with get_audit_recorder(request).operation(entry) as audit_op:
        updated_account, changed = await pause_svc.pause_account(account_id, paused_by=actor)
        acct = updated_account or account
        audit_op.set_outcome(
            AuditResult.SUCCEEDED,
            after={
                "trading_paused": acct.trading_paused,
                "paused_by": acct.paused_by,
                "paused_at": acct.paused_at,
                "state_changed": changed,
            },
        )
    return TradingPauseResponse(
        account_id=acct.id,
        ibkr_account=acct.ibkr_account,
        trading_paused=acct.trading_paused,
        paused_at=acct.paused_at,
        paused_by=acct.paused_by,
    )


@router.post(
    "/accounts/{account_id}/trading-pause/clear",
    response_model=TradingPauseResponse,
    summary="Resume trading and allow new opening signals for an account",
)
async def resume_account_trading_endpoint(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> TradingPauseResponse:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        from app.db.session import AsyncSessionLocal

        session_factory = AsyncSessionLocal

    from app.services.trading_pause import TradingPauseService

    pause_svc = TradingPauseService(session_factory)
    entry = audit_entry(
        request,
        action=AuditAction.TRADING_RESUMED,
        actor=actor_for_user(request, current_user),
        summary=f"Trading resumed on {account.ibkr_account}",
        account_id=account_id,
        ibkr_account=account.ibkr_account,
        target_type="ACCOUNT",
        target_id=account.ibkr_account,
        parameters={"account_id": account_id},
        before_state={"trading_paused": account.trading_paused, "paused_by": account.paused_by},
    )
    async with get_audit_recorder(request).operation(entry) as audit_op:
        updated_account, changed = await pause_svc.resume_account(account_id)
        acct = updated_account or account
        audit_op.set_outcome(
            AuditResult.SUCCEEDED,
            after={"trading_paused": acct.trading_paused, "state_changed": changed},
        )
    return TradingPauseResponse(
        account_id=acct.id,
        ibkr_account=acct.ibkr_account,
        trading_paused=acct.trading_paused,
        paused_at=acct.paused_at,
        paused_by=acct.paused_by,
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

    from app.db.repositories.position_repository import PositionRepository
    from app.services.position_close_service import SinglePairCloseService

    position = await PositionRepository(session).get_by_trade_id(trade_id, account_id=account_id)
    account = await session.get(AccountModel, account_id)
    before = (
        model_snapshot(position, exclude=("live_pnl",))  # live_pnl is a derived mark
        if position is not None
        else None
    )
    symbols = (
        [s for s in (position.leg_a_symbol, position.leg_b_symbol) if s] if position else []
    )
    entry = audit_entry(
        request,
        action=AuditAction.CLOSE_PAIR,
        actor=actor_for_user(request, current_user),
        summary=f"Close Pair {trade_id} ({' / '.join(symbols) or 'unknown legs'})",
        account_id=account_id,
        ibkr_account=account.ibkr_account if account else None,
        target_type="POSITION",
        target_id=trade_id,
        parameters={"account_id": account_id, "trade_id": trade_id, "order_type": "MARKET"},
        before_state=before,
        related={"trade_id": trade_id, "symbols": symbols},
    )

    close_svc = SinglePairCloseService(
        session_factory=session_factory,
        order_manager=order_manager,
    )
    async with get_audit_recorder(request).operation(entry) as audit_op:
        response = await close_svc.close_pair(account_id=account_id, trade_id=trade_id)
        audit_op.add_related(internal_order_ids=response.order_ids or None)
        close_result = {
            "CLOSED": AuditResult.SUCCEEDED,
            "PARTIAL": AuditResult.PARTIAL,
        }.get(response.status, AuditResult.SUCCEEDED if response.success else AuditResult.FAILED)
        audit_op.set_outcome(
            close_result,
            reason=None if response.success else response.message,
            after=response,
        )
    return response


def _position_exits_schema(row) -> PositionExitsSchema:
    return PositionExitsSchema(
        account_id=row.account_id,
        trade_id=row.trade_id,
        risk_state=row.risk_state,
        target=row.target,
        stop=row.stop,
        time_limit=row.time_limit,
        target_unit=getattr(row, "target_unit", None) or "ABSOLUTE",
        stop_unit=getattr(row, "stop_unit", None) or "ABSOLUTE",
        exit_automation_enabled=bool(getattr(row, "exit_automation_enabled", False)),
    )


def _dec_str(value) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _settings_entry(
    request: Request,
    current_user: UserModel,
    *,
    action: AuditAction,
    summary: str,
    account: AccountModel | None = None,
    account_id: int | None = None,
    target_type: str,
    target_id: str | None,
    parameters: object = None,
    before: object = None,
):
    """Audit entry for a single-transaction configuration mutation."""
    return audit_entry(
        request,
        action=action,
        actor=actor_for_user(request, current_user),
        summary=summary,
        account_id=account.id if account is not None else account_id,
        ibkr_account=account.ibkr_account if account is not None else None,
        target_type=target_type,
        target_id=target_id,
        parameters=parameters,
        before_state=before,
    )


def _changes_summary(prefix: str, before: dict | None, after: dict | None) -> str:
    changed = [k for k in changed_fields(before, after) if k != "updated_at"]
    return f"{prefix}: {', '.join(changed)}" if changed else f"{prefix} (no effective change)"


@router.patch(
    "/accounts/{account_id}/positions/{trade_id}/exits",
    response_model=PositionExitsSchema,
    summary="Set or change stop/target on an open pair",
)
async def patch_position_exits(
    account_id: int,
    trade_id: str,
    body: PatchPositionExitsRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> PositionExitsSchema:
    """Update frozen exit knobs on one OPEN positions row. Takes effect on the next monitor tick."""
    _check_account_authorization(current_user, account_id=account_id)
    from app.db.repositories.event_repository import EventRepository
    from app.db.repositories.position_repository import (
        PositionNotFoundError,
        PositionNotOpenError,
        PositionRepository,
    )

    svc = AccountStrategyConfigService(session)
    repo = PositionRepository(session)
    account = await session.get(AccountModel, account_id)
    entry = _settings_entry(
        request,
        current_user,
        action=AuditAction.PAIR_EXITS_UPDATED,
        summary=f"Pair {trade_id} stop/target update",
        account=account,
        account_id=account_id,
        target_type="POSITION",
        target_id=trade_id,
        parameters=body.model_dump(exclude_unset=True),
    )
    entry.related = {"trade_id": trade_id}
    async with get_audit_recorder(request).transaction(entry) as tx:
        row = await repo.get_by_trade_id(trade_id, account_id=account_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Position {trade_id} not found for account {account_id}.",
            )
        if row.risk_state != "OPEN":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"POSITION_NOT_OPEN: trade_id '{trade_id}' is {row.risk_state}; "
                    "exits are immutable after close."
                ),
            )
        if (
            body.target is None
            and body.stop is None
            and body.target_unit is None
            and body.stop_unit is None
            and body.exit_automation_enabled is None
        ):
            raise HTTPException(status_code=400, detail="No fields to update.")

        try:
            new_target_unit = row.target_unit
            new_stop_unit = row.stop_unit
            if body.target_unit is not None:
                new_target_unit = svc.validate_exit_unit(body.target_unit)
            if body.stop_unit is not None:
                new_stop_unit = svc.validate_exit_unit(body.stop_unit)
            if body.target is not None:
                svc.validate_exit_threshold(body.target, new_target_unit, field="target")
            elif body.target_unit is not None:
                svc.validate_exit_threshold(row.target, new_target_unit, field="target")
            if body.stop is not None:
                svc.validate_exit_threshold(body.stop, new_stop_unit, field="stop")
            elif body.stop_unit is not None:
                svc.validate_exit_threshold(row.stop, new_stop_unit, field="stop")
        except AllocationConfigError as exc:
            raise _config_error(exc) from exc

        old = {
            "target": _dec_str(row.target),
            "stop": _dec_str(row.stop),
            "target_unit": row.target_unit,
            "stop_unit": row.stop_unit,
            "exit_automation_enabled": bool(getattr(row, "exit_automation_enabled", False)),
        }
        entry.before_state = old
        try:
            updated = await repo.update_exit_thresholds(
                account_id=account_id,
                trade_id=trade_id,
                target=body.target,
                stop=body.stop,
                target_unit=new_target_unit if body.target_unit is not None else None,
                stop_unit=new_stop_unit if body.stop_unit is not None else None,
                exit_automation_enabled=body.exit_automation_enabled,
            )
        except PositionNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Position {trade_id} not found for account {account_id}.",
            ) from exc
        except PositionNotOpenError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        new = {
            "target": _dec_str(updated.target),
            "stop": _dec_str(updated.stop),
            "target_unit": updated.target_unit,
            "stop_unit": updated.stop_unit,
            "exit_automation_enabled": updated.exit_automation_enabled,
        }
        await EventRepository(session).append(
            process="config",
            kind="PAIR_EXIT_THRESHOLDS_UPDATED",
            detail={
                "account_id": account_id,
                "trade_id": trade_id,
                "old": old,
                "new": new,
            },
        )
        await tx.commit(
            session,
            after=new,
            summary=_changes_summary(f"Pair {trade_id} exits updated", old, new),
        )
    logger.info(
        "Config PATCH position exits account_id=%s trade_id=%s old=%s new=%s",
        account_id,
        trade_id,
        old,
        new,
    )
    return _position_exits_schema(updated)


@router.post(
    "/accounts",
    response_model=AccountConfigSchema,
    status_code=201,
    summary="Create a new paper trading account",
)
async def create_account(
    body: CreateAccountRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: UserModel = Depends(require_admin),
) -> AccountConfigSchema:
    svc = AccountStrategyConfigService(session)
    entry = _settings_entry(
        request,
        _admin,
        action=AuditAction.ACCOUNT_CREATED,
        summary=f"Trading account {body.ibkr_account} created",
        target_type="ACCOUNT",
        target_id=body.ibkr_account,
        parameters=body.model_dump(),
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
        try:
            account = await svc.create_account(
                name=body.name,
                ibkr_account=body.ibkr_account,
                total_margin=body.total_margin,
                enabled=body.enabled,
                default_symbol_limit=body.default_symbol_limit,
            )
            await session.flush()
            await tx.commit(
                session,
                after=model_snapshot(account),
                account_id=account.id,
                ibkr_account=account.ibkr_account,
            )
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
        trading_paused=account.trading_paused,
        paused_at=account.paused_at,
        paused_by=account.paused_by,
        **_account_risk_fields(account),
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
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> AccountConfigSchema:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    entry = _settings_entry(
        request,
        current_user,
        action=AuditAction.ACCOUNT_SETTINGS_UPDATED,
        summary=f"Account {account.ibkr_account if account else account_id} settings update",
        account=account,
        account_id=account_id,
        target_type="ACCOUNT",
        target_id=account.ibkr_account if account else str(account_id),
        parameters=body.model_dump(exclude_unset=True),
        before=model_snapshot(account),
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
        # loss_threshold needs sentinel to distinguish omitted vs explicit null
        has_loss = "loss_threshold" in body.model_fields_set
        has_cancel_exposure = "cancel_exposure" in body.model_fields_set
        if (
            body.name is None
            and body.ibkr_account is None
            and body.total_margin is None
            and body.enabled is None
            and body.default_symbol_limit is None
            and body.daily_target is None
            and body.daily_stop is None
            and body.daily_target_unit is None
            and body.daily_stop_unit is None
            and body.account_risk_enabled is None
            and not has_loss
            and not has_cancel_exposure
        ):
            raise HTTPException(status_code=400, detail="No fields to update.")
        kwargs: dict = {}
        for field_name in body.model_fields_set:
            val = getattr(body, field_name, None)
            if val is not None or field_name == "loss_threshold":
                kwargs[field_name] = val

        try:
            await svc.update_account(account, **kwargs)
            await session.flush()
            after = model_snapshot(account)
            await tx.commit(
                session,
                after=after,
                summary=_changes_summary(
                    f"Account {account.ibkr_account} settings updated", entry.before_state, after
                ),
            )
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
        trading_paused=account.trading_paused,
        paused_at=account.paused_at,
        paused_by=account.paused_by,
        **_account_risk_fields(account),
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
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: UserModel = Depends(require_admin),
) -> AllocationConfigSchema:
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    entry = _settings_entry(
        request,
        _admin,
        action=AuditAction.ALLOCATION_CREATED,
        summary=f"Strategy allocation {body.strategy_id} created on account {account_id}",
        account=account,
        account_id=account_id,
        target_type="ALLOCATION",
        target_id=body.strategy_id,
        parameters=body.model_dump(),
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
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
                target_unit=body.target_unit,
                stop_unit=body.stop_unit,
                exit_automation_enabled=body.exit_automation_enabled,
            )
            await session.flush()
            await tx.commit(
                session,
                after=model_snapshot(allocation),
                related={"allocation_id": allocation.id},
            )
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
    before: dict | None = None
    if account is not None:
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
        before = {
            "account": model_snapshot(account),
            "allocations": [model_snapshot(a) for a in allocations],
            "symbol_limits": [model_snapshot(lim) for lim in limits],
        }
    entry = _settings_entry(
        request,
        _admin,
        action=AuditAction.ACCOUNT_DELETED,
        summary=f"Trading account {account.ibkr_account if account else account_id} deleted",
        account=account,
        account_id=account_id,
        target_type="ACCOUNT",
        target_id=account.ibkr_account if account else str(account_id),
        parameters={"account_id": account_id},
        before=before,
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
        try:
            await svc.delete_account(account_id)
            await tx.commit(session, after={"deleted": True})
        except AllocationConfigError as exc:
            await session.rollback()
            raise _config_error(exc) from exc

    order_manager: OrderManager | None = getattr(request.app.state, "order_manager", None)
    if order_manager is not None:
        await order_manager.reload_rms_limits()

    from app.services.kill_switch import clear_account_kill_switch_cache

    # A deleted account must not leave stale ids in any scope's hot cache.
    clear_account_kill_switch_cache(account_id)

    logger.info("Config DELETE account id=%s", account_id)


@router.patch(
    "/allocations/{allocation_id}",
    response_model=AllocationConfigSchema,
    summary="Update allocation pct, enabled, or position cap",
)
async def patch_allocation(
    allocation_id: int,
    body: PatchAllocationRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(require_authenticated_user),
) -> AllocationConfigSchema:
    svc = AccountStrategyConfigService(session)
    allocation = await svc.get_allocation(allocation_id)
    if allocation is not None:
        _check_account_authorization(current_user, account_id=allocation.account_id)
    account = (
        await session.get(AccountModel, allocation.account_id) if allocation is not None else None
    )
    entry = _settings_entry(
        request,
        current_user,
        action=AuditAction.ALLOCATION_UPDATED,
        summary=(
            f"Strategy allocation {allocation.strategy_id} update"
            if allocation is not None
            else f"Strategy allocation {allocation_id} update"
        ),
        account=account,
        target_type="ALLOCATION",
        target_id=allocation.strategy_id if allocation is not None else str(allocation_id),
        parameters=body.model_dump(exclude_unset=True),
        before=model_snapshot(allocation),
    )
    entry.related = {"allocation_id": allocation_id}
    async with get_audit_recorder(request).transaction(entry) as tx:
        if allocation is None:
            raise HTTPException(status_code=404, detail=f"Allocation {allocation_id} not found.")
        if (
            body.alloc_pct is None
            and body.enabled is None
            and body.max_open_positions is None
            and body.pair_max_allocation_pct is None
            and body.target is None
            and body.stop is None
            and body.time_limit is None
            and body.target_unit is None
            and body.stop_unit is None
            and body.exit_automation_enabled is None
        ):
            raise HTTPException(status_code=400, detail="No fields to update.")
        try:
            await svc.update_allocation(
                allocation,
                alloc_pct=body.alloc_pct,
                enabled=body.enabled,
                max_open_positions=body.max_open_positions,
                pair_max_allocation_pct=body.pair_max_allocation_pct,
                target=body.target,
                stop=body.stop,
                time_limit=body.time_limit,
                target_unit=body.target_unit,
                stop_unit=body.stop_unit,
                exit_automation_enabled=body.exit_automation_enabled,
            )
            await session.flush()
            after = model_snapshot(allocation)
            await tx.commit(
                session,
                after=after,
                summary=_changes_summary(
                    f"Strategy allocation {allocation.strategy_id} updated",
                    entry.before_state,
                    after,
                ),
            )
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


async def _symbol_limit_row(
    session: AsyncSession, account_id: int, symbol: str
) -> PerSymbolLimitModel | None:
    return (
        await session.execute(
            select(PerSymbolLimitModel).where(
                PerSymbolLimitModel.account_id == account_id,
                PerSymbolLimitModel.symbol == symbol.strip().upper(),
            )
        )
    ).scalar_one_or_none()


@router.put(
    "/accounts/{account_id}/symbol-limits/{symbol}",
    response_model=SymbolLimitSchema,
    summary="Upsert per-symbol money limit",
)
async def put_symbol_limit(
    account_id: int,
    symbol: str,
    body: PutSymbolLimitRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
    current_user: UserModel = Depends(require_authenticated_user),
) -> SymbolLimitSchema:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await session.get(AccountModel, account_id)
    existing = await _symbol_limit_row(session, account_id, symbol)
    entry = _settings_entry(
        request,
        current_user,
        action=AuditAction.SYMBOL_LIMIT_SET,
        summary=f"Symbol limit {symbol.strip().upper()} set to {body.money_limit}",
        account=account,
        account_id=account_id,
        target_type="SYMBOL_LIMIT",
        target_id=symbol.strip().upper(),
        parameters={"symbol": symbol, **body.model_dump()},
        before=model_snapshot(existing),
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
        try:
            row = await svc.upsert_symbol_limit(
                account_id=account_id,
                symbol=symbol,
                money_limit=body.money_limit,
            )
            await session.flush()
            await tx.commit(session, after=model_snapshot(row))
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
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
    current_user: UserModel = Depends(require_authenticated_user),
) -> AccountConfigSchema:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await svc.get_account(account_id)
    entry = _settings_entry(
        request,
        current_user,
        action=AuditAction.DEFAULT_SYMBOL_LIMIT_UPDATED,
        summary=f"Default symbol limit set to {body.default_symbol_limit}",
        account=account,
        account_id=account_id,
        target_type="ACCOUNT",
        target_id=account.ibkr_account if account else str(account_id),
        parameters=body.model_dump(),
        before={"default_symbol_limit": account.default_symbol_limit} if account else None,
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
        try:
            await svc.update_account(account, default_symbol_limit=body.default_symbol_limit)
            await tx.commit(
                session, after={"default_symbol_limit": account.default_symbol_limit}
            )
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
        trading_paused=account.trading_paused,
        paused_at=account.paused_at,
        paused_by=account.paused_by,
        **_account_risk_fields(account),
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
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
    current_user: UserModel = Depends(require_authenticated_user),
) -> None:
    _check_account_authorization(current_user, account_id=account_id)
    svc = AccountStrategyConfigService(session)
    account = await session.get(AccountModel, account_id)
    existing = await _symbol_limit_row(session, account_id, symbol)
    entry = _settings_entry(
        request,
        current_user,
        action=AuditAction.SYMBOL_LIMIT_REMOVED,
        summary=f"Symbol limit {symbol.strip().upper()} removed",
        account=account,
        account_id=account_id,
        target_type="SYMBOL_LIMIT",
        target_id=symbol.strip().upper(),
        parameters={"symbol": symbol},
        before=model_snapshot(existing),
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
        deleted = await svc.delete_symbol_limit(account_id=account_id, symbol=symbol)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Symbol limit {symbol!r} not found for account {account_id}.",
            )
        await tx.commit(session, after={"deleted": True})
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
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
    _admin: UserModel = Depends(require_admin),
) -> ExecutionSettingsSchema:
    svc = AccountStrategyConfigService(session)
    before_row = await svc.get_or_create_execution_settings()
    entry = _settings_entry(
        request,
        _admin,
        action=AuditAction.EXECUTION_SETTINGS_UPDATED,
        summary="Execution settings update",
        target_type="EXECUTION_SETTINGS",
        target_id="global",
        parameters=body.model_dump(exclude_unset=True),
        before=model_snapshot(before_row),
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
        if (
            body.enabled is None
            and body.square_off_after_sec is None
            and body.max_retries is None
            and body.retry_interval_sec is None
            and body.retry_window_sec is None
        ):
            raise HTTPException(status_code=400, detail="No fields to update.")
        try:
            row = await svc.update_execution_settings(
                enabled=body.enabled,
                square_off_after_sec=body.square_off_after_sec,
                max_retries=body.max_retries,
                retry_interval_sec=body.retry_interval_sec,
                retry_window_sec=body.retry_window_sec,
            )
            await session.flush()
            after = model_snapshot(row)
            await tx.commit(
                session,
                after=after,
                summary=_changes_summary("Execution settings updated", entry.before_state, after),
            )
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
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    order_manager: OrderManager = Depends(get_order_manager),
    _admin: UserModel = Depends(require_admin),
) -> MarginSettingsSchema:
    svc = AccountStrategyConfigService(session)
    before_row = await svc.get_or_create_margin_settings()
    entry = _settings_entry(
        request,
        _admin,
        action=AuditAction.MARGIN_SETTINGS_UPDATED,
        summary="Margin controls update",
        target_type="MARGIN_SETTINGS",
        target_id="global",
        parameters=body.model_dump(exclude_unset=True),
        before=model_snapshot(before_row),
    )
    async with get_audit_recorder(request).transaction(entry) as tx:
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
            await session.flush()
            after = model_snapshot(row)
            await tx.commit(
                session,
                after=after,
                summary=_changes_summary("Margin controls updated", entry.before_state, after),
            )
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
