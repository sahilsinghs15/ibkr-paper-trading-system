"""Operator audit trail: server-side search, event detail and investigation context.

Read-only and admin-only. There are deliberately no write/modify/delete routes;
audit rows are produced exclusively by the audited backend operations and the
database rejects UPDATE/DELETE of finalized rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.audit.recorder import values_equivalent
from app.audit.taxonomy import (
    ACTION_CATALOG,
    CATEGORY_LABELS,
    ActorType,
    AuditAction,
    AuditCategory,
    AuditResult,
)
from app.db.models.audit import AuditEventModel, AuthSessionModel
from app.db.models.user import UserModel
from app.db.repositories.audit_repository import (
    AuditRepository,
    AuditSearchFilters,
    parse_ip_filter,
)
from app.db.session import get_db_session
from app.schemas.audit_schemas import (
    AuditActionFacet,
    AuditCategoryFacet,
    AuditEventDetail,
    AuditEventsResponse,
    AuditEventSummary,
    AuditFacetsResponse,
    AuthSessionInfo,
    FieldChange,
    InvestigationContext,
)

router = APIRouter(prefix="/audit", tags=["audit"])

_ACTION_LABELS = {action.value: label for action, (_, label) in ACTION_CATALOG.items()}
_VALID_CATEGORIES = {c.value for c in AuditCategory}
_VALID_ACTIONS = {a.value for a in AuditAction}
_VALID_RESULTS = {r.value for r in AuditResult}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _clean(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def _validated(values: list[str] | None, allowed: set[str], name: str) -> list[str]:
    cleaned = [v.strip().upper() for v in values or [] if v and v.strip()]
    invalid = sorted(set(cleaned) - allowed)
    if invalid:
        raise HTTPException(status_code=422, detail=f"Unknown {name}: {', '.join(invalid)}")
    return cleaned


def _client_label(context: dict[str, Any]) -> str | None:
    client = context.get("client") if isinstance(context, dict) else None
    if not isinstance(client, dict):
        return None
    parts = [
        " ".join(p for p in (client.get("browser"), client.get("browser_version")) if p),
        client.get("os"),
    ]
    label = " / ".join(p for p in parts if p)
    return label or None


def _summary(row: AuditEventModel) -> AuditEventSummary:
    return AuditEventSummary(
        event_id=row.event_id,
        occurred_at=row.occurred_at,
        category=row.category,
        action=row.action,
        action_label=_ACTION_LABELS.get(row.action, row.action),
        result=row.result,
        summary=row.summary,
        actor_type=row.actor_type,
        actor_user_id=row.actor_user_id,
        actor_email=row.actor_email,
        actor_role=row.actor_role,
        session_id=row.session_id,
        client_ip=str(row.client_ip) if row.client_ip is not None else None,
        client_label=_client_label(row.context),
        client_device_id=row.client_device_id,
        account_id=row.account_id,
        ibkr_account=row.ibkr_account,
        target_type=row.target_type,
        target_id=row.target_id,
        provenance=row.provenance,
    )


def _session_info(row: AuthSessionModel) -> AuthSessionInfo:
    return AuthSessionInfo(
        session_id=row.id,
        user_id=row.user_id,
        user_email=row.user_email,
        user_role=row.user_role,
        auth_method=row.auth_method,
        created_at=row.created_at,
        expires_at=row.expires_at,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
        login_ip=str(row.login_ip) if row.login_ip is not None else None,
        login_user_agent=row.login_user_agent,
        login_device_id=row.login_device_id,
        login_audit_event_id=row.login_audit_event_id,
    )


def _changes(before: Any, after: Any) -> list[FieldChange] | None:
    if not isinstance(before, dict) and not isinstance(after, dict):
        return None
    before_d = before if isinstance(before, dict) else {}
    after_d = after if isinstance(after, dict) else {}
    keys = sorted(set(before_d) | set(after_d))
    return [
        FieldChange(field=k, before=before_d.get(k), after=after_d.get(k))
        for k in keys
        if not values_equivalent(before_d.get(k), after_d.get(k))
    ]


@router.get("/events", response_model=AuditEventsResponse, summary="Search the operator audit trail")
async def search_audit_events(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[UserModel, Depends(require_admin)],
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    actor: Annotated[str | None, Query(max_length=255)] = None,
    actor_user_id: Annotated[int | None, Query(ge=1)] = None,
    role: Annotated[str | None, Query(max_length=50)] = None,
    ip: Annotated[str | None, Query(max_length=64, description="IP address or CIDR")] = None,
    category: Annotated[list[str] | None, Query()] = None,
    action: Annotated[list[str] | None, Query()] = None,
    result: Annotated[list[str] | None, Query()] = None,
    account: Annotated[str | None, Query(max_length=64)] = None,
    session_id: Annotated[uuid.UUID | None, Query()] = None,
    device_id: Annotated[str | None, Query(max_length=64)] = None,
    target_type: Annotated[str | None, Query(max_length=48)] = None,
    target_id: Annotated[str | None, Query(max_length=128)] = None,
    ref_id: Annotated[str | None, Query(max_length=128)] = None,
    order_id: Annotated[str | None, Query(max_length=128)] = None,
    trade_id: Annotated[str | None, Query(max_length=128)] = None,
    position_id: Annotated[str | None, Query(max_length=128)] = None,
    correlation_id: Annotated[str | None, Query(max_length=128)] = None,
    browser: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=200, description="Keyword")] = None,
    provenance: Annotated[Literal["all", "native", "legacy"], Query()] = "all",
    sort: Annotated[Literal["newest", "oldest"], Query()] = "newest",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> AuditEventsResponse:
    date_from = _as_utc(date_from)
    date_to = _as_utc(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")
    if ip:
        try:
            parse_ip_filter(ip)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid IP/CIDR filter: {ip!r}") from exc

    filters = AuditSearchFilters(
        date_from=date_from,
        date_to=date_to,
        actor=actor.strip() if actor and actor.strip() else None,
        actor_user_id=actor_user_id,
        actor_role=role.strip() if role and role.strip() else None,
        client_ip=ip.strip() if ip and ip.strip() else None,
        categories=_validated(category, _VALID_CATEGORIES, "category"),
        actions=_validated(action, _VALID_ACTIONS, "action"),
        results=_validated(result, _VALID_RESULTS, "result"),
        account=account.strip() if account and account.strip() else None,
        session_id=session_id,
        device_id=device_id.strip() if device_id and device_id.strip() else None,
        target_type=target_type.strip().upper() if target_type and target_type.strip() else None,
        target_id=target_id.strip() if target_id and target_id.strip() else None,
        ref_id=_clean(ref_id),
        order_id=_clean(order_id),
        trade_id=_clean(trade_id),
        position_id=_clean(position_id),
        correlation_id=_clean(correlation_id),
        browser=_clean(browser),
        keyword=q.strip() if q and q.strip() else None,
        provenance=provenance,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    rows, total = await AuditRepository(session).search(filters)
    return AuditEventsResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[_summary(r) for r in rows],
    )


@router.get(
    "/events/{event_id}",
    response_model=AuditEventDetail,
    summary="Full audit event with investigation context",
)
async def get_audit_event(
    event_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[UserModel, Depends(require_admin)],
) -> AuditEventDetail:
    repo = AuditRepository(session)
    row = await repo.get(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Audit event not found")

    investigation = InvestigationContext()
    if row.session_id is not None:
        auth_session = await repo.get_auth_session(row.session_id)
        if auth_session is not None:
            investigation.session = _session_info(auth_session)
        investigation.session_event_count = await repo.session_event_count(row.session_id)
    if row.actor_user_id is not None:
        others = await repo.concurrent_sessions(row.actor_user_id, row.occurred_at, row.session_id)
        investigation.concurrent_sessions = [_session_info(s) for s in others]
        if row.client_device_id:
            investigation.device_first_seen_at = await repo.device_first_seen(
                row.actor_user_id, row.client_device_id
            )
        investigation.actor_ips_within_24h = [
            {"ip": ip, "events": n}
            for ip, n in await repo.recent_ips_for_user(
                row.actor_user_id, row.occurred_at, timedelta(hours=24)
            )
        ]

    base = _summary(row).model_dump()
    return AuditEventDetail(
        **base,
        recorded_at=row.recorded_at,
        completed_at=row.completed_at,
        result_reason=row.result_reason,
        auth_method=row.auth_method,
        user_agent=row.user_agent,
        request_id=row.request_id,
        correlation_id=row.correlation_id,
        http_method=row.http_method,
        http_path=row.http_path,
        parameters=row.parameters or {},
        before_state=row.before_state,
        after_state=row.after_state,
        changes=_changes(row.before_state, row.after_state),
        related=row.related or {},
        ref_ids=list(row.ref_ids or []),
        context=row.context or {},
        legacy_ref=row.legacy_ref,
        investigation=investigation,
    )


@router.get("/facets", response_model=AuditFacetsResponse, summary="Filter vocabularies")
async def get_audit_facets(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[UserModel, Depends(require_admin)],
) -> AuditFacetsResponse:
    categories = [
        AuditCategoryFacet(
            category=cat.value,
            label=CATEGORY_LABELS[cat],
            actions=[
                AuditActionFacet(action=action.value, label=label)
                for action, (action_cat, label) in ACTION_CATALOG.items()
                if action_cat is cat
            ],
        )
        for cat in AuditCategory
    ]
    known = await AuditRepository(session).distinct_values()
    return AuditFacetsResponse(
        categories=categories,
        results=[r.value for r in AuditResult],
        actor_types=[a.value for a in ActorType],
        actors=known["actors"],
        roles=known["roles"],
        browsers=known["browsers"],
        target_types=known["target_types"],
    )
