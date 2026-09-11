"""API routes for Manual Trading.

Supports CFD instrument search, pre-trade preview, durable order placement (M1-B),
order status / execution tracking (M1-C), manual position ledger (M1-D), and
safe broker order cancellation (M1-E).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from app.api.deps import require_authenticated_user
from app.api.routes.config import _check_account_authorization
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import get_settings
from app.db.models.account import AccountModel
from app.db.models.user import UserModel
from app.db.repositories.manual_repository import (
    ManualHaltRepository,
    ManualOrderRepository,
    ManualPositionRepository,
)
from app.db.session import AsyncSessionLocal
from app.instruments.cfd_discover import cfd_search_contract
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.schemas.manual_schemas import (
    CfdCandidateContract,
    CfdSearchRequest,
    CfdSearchResponse,
    GatewayEnvironmentMode,
    GatewayModeVerificationState,
    GatewayStatusResponse,
    ManualHaltStateRead,
    ManualOrderCancelResponse,
    ManualOrderPreviewRequest,
    ManualOrderPreviewResponse,
    ManualOrderRead,
    ManualOrdersListResponse,
    ManualOrderSubmitRequest,
    ManualOrderSubmitResponse,
    ManualPositionRead,
    ManualPositionsListResponse,
)
from app.services.manual_trading import ManualTradingService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manual", tags=["manual"])


def _determine_gateway_mode(
    client: TWSClient | None, port: int, managed_accounts: list[str]
) -> tuple[GatewayEnvironmentMode, GatewayModeVerificationState, str]:
    """Authoritatively evaluate whether the connection is Paper or Live.

    Safety Rule: Port alone (e.g. 4002/7497) is NEVER claimed as 'VERIFIED_PAPER'.
    Authoritative verification requires verified account prefix ('DU' / 'DF' for paper)
    from broker runtime callbacks.
    """
    if client is None or not client.is_connected():
        return (
            GatewayEnvironmentMode.UNKNOWN,
            GatewayModeVerificationState.UNVERIFIED,
            "Gateway disconnected. Environment unknown.",
        )

    # 1. Authoritative check: IBKR Paper accounts have prefix 'DU', 'DF', etc.
    # While Live accounts start with 'U' followed by digits.
    if managed_accounts:
        has_paper_account = any(
            acc.startswith(("DU", "DF")) for acc in managed_accounts
        )
        has_live_account = any(
            acc.startswith("U") and not acc.startswith(("DU", "DF"))
            for acc in managed_accounts
        )
        if has_paper_account and not has_live_account:
            return (
                GatewayEnvironmentMode.VERIFIED_PAPER,
                GatewayModeVerificationState.ACCOUNT_PREFIX_VERIFIED,
                f"Authoritatively verified PAPER via IBKR managed accounts: {', '.join(managed_accounts)}",
            )
        if has_live_account and not has_paper_account:
            return (
                GatewayEnvironmentMode.VERIFIED_LIVE,
                GatewayModeVerificationState.ACCOUNT_PREFIX_VERIFIED,
                f"Authoritatively verified LIVE via IBKR managed accounts: {', '.join(managed_accounts)}",
            )
        if has_paper_account and has_live_account:
            return (
                GatewayEnvironmentMode.UNKNOWN,
                GatewayModeVerificationState.UNVERIFIED,
                f"Ambiguous mixed paper and live accounts detected: {', '.join(managed_accounts)}",
            )

    # 2. If managed accounts are not yet populated or ambiguous, inspect configured port as expected hint only
    if port in (4002, 7497):
        return (
            GatewayEnvironmentMode.UNKNOWN,
            GatewayModeVerificationState.EXPECTED_PAPER_BY_CONFIGURATION,
            f"Configured port {port} matches expected Paper Gateway/TWS, but runtime account prefix is unconfirmed.",
        )
    if port in (4001, 7496):
        return (
            GatewayEnvironmentMode.UNKNOWN,
            GatewayModeVerificationState.EXPECTED_LIVE_BY_CONFIGURATION,
            f"Configured port {port} matches expected Live Gateway/TWS, but runtime account prefix is unconfirmed.",
        )

    # 3. Fallback for non-standard ports or unverified/mixed environments
    acc_desc = f" (managed accounts: {', '.join(managed_accounts)})" if managed_accounts else ""
    return (
        GatewayEnvironmentMode.UNKNOWN,
        GatewayModeVerificationState.UNVERIFIED,
        f"Gateway connected on port {port}, but environment is unverified{acc_desc}.",
    )


def is_connection_verified_paper(
    client: TWSClient | None,
    port: int,
    managed_accounts: list[str] | None = None,
) -> tuple[bool, str]:
    """Diagnostic environment verification for operational visibility.

    Evaluates whether the active Gateway connection authoritatively maps to an IBKR Paper account.
    Per Invariant 1 (Environment-Agnostic Manual Trading), this is for UI diagnostics, operational
    visibility, and safety checks, and does not hard-block live Gateway executions.
    """
    if client is None or not client.is_connected():
        return False, "Gateway is disconnected"
    accs = (
        managed_accounts
        if managed_accounts is not None
        else (sorted(client.managed_accounts) if client else [])
    )
    mode, state, msg = _determine_gateway_mode(client, port, accs)
    if (
        mode == GatewayEnvironmentMode.VERIFIED_PAPER
        and state == GatewayModeVerificationState.ACCOUNT_PREFIX_VERIFIED
    ):
        return True, msg
    return False, f"Not verified paper environment (mode={mode.value}, state={state.value}): {msg}"


@router.get(
    "/gateway-status",
    summary="Get safe operational IBKR Gateway status",
    response_model=GatewayStatusResponse,
)
async def get_gateway_status(
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str | None, Query()] = None,
) -> GatewayStatusResponse:
    """Return read-only, safe operational status of the single TWSClient Gateway connection.

    Never exposes passwords, tokens, or credentials.
    """
    settings = get_settings()
    client: TWSClient | None = getattr(request.app.state, "client", None)
    connected = client is not None and client.is_connected()

    managed_accs: list[str] = sorted(client.managed_accounts) if client else []
    clean_account = ibkr_account.strip().upper() if ibkr_account else None

    if clean_account:
        _check_account_authorization(current_user, ibkr_account=clean_account)

    env_mode, verif_state, msg = _determine_gateway_mode(
        client, settings.ibkr_port, managed_accs
    )

    server_version = getattr(client, "serverVersion", lambda: None)() if client and connected else None
    conn_time = getattr(client, "twsConnectionTime", lambda: None)() if client and connected else None
    conn_time_str = conn_time.decode() if isinstance(conn_time, bytes) else (str(conn_time) if conn_time else None)

    return GatewayStatusResponse(
        connected=connected,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        ibkr_account=clean_account,
        managed_accounts=managed_accs,
        environment=env_mode,
        verification_state=verif_state,
        server_version=server_version,
        connection_time=conn_time_str,
        message=msg,
    )


@router.post(
    "/instruments/search",
    summary="Search for CFD contract candidates via IBKR reqContractDetails",
    response_model=CfdSearchResponse,
)
async def search_cfd_instruments(
    request: Request,
    payload: CfdSearchRequest,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
) -> CfdSearchResponse:
    """Discover all valid CFD contract candidates for the requested symbol.

    Reuses existing single TWSClient and GatewayRateLimiter.
    Never auto-selects a single contract or calls pick_unique_cfd_details().
    Returns ALL candidates for explicit user selection.
    """
    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)

    client: TWSClient | None = getattr(request.app.state, "client", None)
    if client is None or not client.is_connected():
        raise HTTPException(
            status_code=503,
            detail="IBKR Gateway is disconnected. Cannot search instruments.",
        )

    search_sym = payload.symbol.strip().upper()
    if not search_sym:
        raise HTTPException(status_code=400, detail="Symbol cannot be empty")

    ib_contract = cfd_search_contract(
        symbol=search_sym,
        exchange=payload.exchange,
        currency=payload.currency,
    )

    try:
        raw_details = await client.request_contract_details_async(ib_contract, timeout=7.0)
    except Exception as exc:
        logger.exception("Error during CFD contract details discovery for %s", search_sym)
        raise HTTPException(
            status_code=502, detail="Failed to query contract details from IBKR Gateway."
        ) from exc

    candidates: list[CfdCandidateContract] = []
    for d in raw_details:
        c = getattr(d, "contract", None)
        if c is None:
            continue
        sec_type = (getattr(c, "secType", "") or "").upper()
        if sec_type != "CFD":
            continue
        con_id = int(getattr(c, "conId", 0) or 0)
        if con_id <= 0:
            continue

        min_tick = getattr(d, "minTick", None)
        candidates.append(
            CfdCandidateContract(
                con_id=con_id,
                symbol=(getattr(c, "symbol", "") or "").strip().upper(),
                sec_type="CFD",
                exchange=(getattr(c, "exchange", "") or "").strip().upper() or payload.exchange,
                currency=(getattr(c, "currency", "") or "").strip().upper() or payload.currency,
                local_symbol=getattr(c, "localSymbol", None),
                trading_class=getattr(c, "tradingClass", None),
                min_tick=float(min_tick) if min_tick is not None else None,
                primary_exchange=getattr(c, "primaryExchange", None),
                long_name=getattr(d, "longName", None),
            )
        )

    # Sort candidates by exchange (SMART first), then con_id
    candidates.sort(
        key=lambda x: (0 if x.exchange == "SMART" else 1, x.exchange, x.con_id)
    )

    msg = f"Found {len(candidates)} CFD candidate(s) for {search_sym}."
    if len(candidates) == 0:
        msg = f"No CFD contracts found on IBKR for symbol '{search_sym}' with currency '{payload.currency}'."

    return CfdSearchResponse(
        symbol=search_sym,
        candidates=candidates,
        count=len(candidates),
        message=msg,
    )


@router.post(
    "/orders/preview",
    summary="Non-mutating manual order pre-trade preview with margin probe",
    response_model=ManualOrderPreviewResponse,
)
async def preview_manual_order(
    request: Request,
    payload: ManualOrderPreviewRequest,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
) -> ManualOrderPreviewResponse:
    """Non-mutating pre-trade order validation, notional calculation, and what-if margin probe."""
    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)

    settings = get_settings()
    client: TWSClient | None = getattr(request.app.state, "client", None)
    ibkr_adapter: IBKRExecutionAdapter | None = getattr(request.app.state, "ibkr_adapter", None)

    managed_accs: list[str] = sorted(client.managed_accounts) if client else []
    env_mode, _, _ = _determine_gateway_mode(client, settings.ibkr_port, managed_accs)

    async with AsyncSessionLocal() as session:
        acc = (
            await session.execute(
                select(AccountModel).where(
                    func.upper(AccountModel.ibkr_account) == clean_account
                )
            )
        ).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")

        service = ManualTradingService(session, client=client, ibkr_adapter=ibkr_adapter)
        return await service.preview_order(acc, payload, env_mode)


@router.post(
    "/orders",
    summary="Submit manual order to IBKR Gateway with durable idempotency",
    response_model=ManualOrderSubmitResponse,
)
async def submit_manual_order(
    request: Request,
    payload: ManualOrderSubmitRequest,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
) -> ManualOrderSubmitResponse:
    """Durable manual order submission.

    Two-phase state machine:
    Validation -> Idempotency check -> PENDING_SUBMIT -> DB COMMIT -> Rate limiter -> placeOrder() -> SUBMITTED.
    """
    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)

    client: TWSClient | None = getattr(request.app.state, "client", None)
    ibkr_adapter: IBKRExecutionAdapter | None = getattr(request.app.state, "ibkr_adapter", None)

    async with AsyncSessionLocal() as session:
        acc = (
            await session.execute(
                select(AccountModel).where(
                    func.upper(AccountModel.ibkr_account) == clean_account
                )
            )
        ).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")

        service = ManualTradingService(session, client=client, ibkr_adapter=ibkr_adapter)
        return await service.submit_order(acc, payload, current_user)


@router.get(
    "/orders",
    summary="List manual orders for an account",
    response_model=ManualOrdersListResponse,
)
async def list_manual_orders(
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
    status: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ManualOrdersListResponse:
    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)

    async with AsyncSessionLocal() as session:
        acc = (
            await session.execute(
                select(AccountModel).where(
                    func.upper(AccountModel.ibkr_account) == clean_account
                )
            )
        ).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")

        repo = ManualOrderRepository(session)
        orders, total = await repo.list_orders_for_account(
            account_id=acc.id,
            status=status,
            symbol=symbol,
            limit=limit,
            offset=offset,
        )

        return ManualOrdersListResponse(
            account_id=acc.id,
            ibkr_account=clean_account,
            source="manual",
            orders=[ManualOrderRead.model_validate(o) for o in orders],
            total=total,
        )


@router.get(
    "/positions",
    summary="List open manual positions for an account",
    response_model=ManualPositionsListResponse,
)
async def list_manual_positions(
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
) -> ManualPositionsListResponse:
    """Return open manual positions querying manual_positions table directly.

    Genuinely source-isolated: queries manual_positions only, never filtered engine positions.
    """
    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)

    async with AsyncSessionLocal() as session:
        acc = (
            await session.execute(
                select(AccountModel).where(
                    func.upper(AccountModel.ibkr_account) == clean_account
                )
            )
        ).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")

        repo = ManualPositionRepository(session)
        positions = await repo.list_open_for_account(acc.id)

        return ManualPositionsListResponse(
            account_id=acc.id,
            ibkr_account=clean_account,
            source="manual",
            positions=[ManualPositionRead.model_validate(p) for p in positions],
            total=len(positions),
        )


@router.get(
    "/halt-state",
    summary="Get manual trading halt state for an account",
    response_model=ManualHaltStateRead,
)
async def get_manual_halt_state(
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
) -> ManualHaltStateRead:
    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)

    async with AsyncSessionLocal() as session:
        acc = (
            await session.execute(
                select(AccountModel).where(
                    func.upper(AccountModel.ibkr_account) == clean_account
                )
            )
        ).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")

        repo = ManualHaltRepository(session)
        state = await repo.get_halt_state(acc.id)
        if state is None:
            return ManualHaltStateRead(
                account_id=acc.id,
                halted=False,
                halted_by=None,
                halted_at=None,
                reason=None,
            )
        return ManualHaltStateRead.model_validate(state)


@router.post(
    "/orders/{order_id}/cancel",
    summary="Cancel working manual order at IBKR Gateway (M1-E)",
    response_model=ManualOrderCancelResponse,
)
async def cancel_manual_order(
    request: Request,
    order_id: int,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
    ibkr_account: Annotated[str, Query(..., min_length=1)],
) -> ManualOrderCancelResponse:
    """Cancel an eligible working manual order.

    Single permitted broker mutation: broker order cancellation dispatched via service through GatewayRateLimiter.
    Zero placeOrder, reqGlobalCancel, emergency_flatten, square_off.
    Enforces authenticated user and authorized account ownership.
    """
    clean_account = ibkr_account.strip().upper()
    _check_account_authorization(current_user, ibkr_account=clean_account)

    client: TWSClient | None = getattr(request.app.state, "client", None)
    ibkr_adapter: IBKRExecutionAdapter | None = getattr(request.app.state, "ibkr_adapter", None)

    async with AsyncSessionLocal() as session:
        acc = (
            await session.execute(
                select(AccountModel).where(
                    func.upper(AccountModel.ibkr_account) == clean_account
                )
            )
        ).scalar_one_or_none()
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Account {clean_account} not found")

        service = ManualTradingService(session, client=client, ibkr_adapter=ibkr_adapter)
        return await service.cancel_order(acc, order_id=order_id, current_user=current_user)

