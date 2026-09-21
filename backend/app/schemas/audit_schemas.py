"""Response schemas for the operator audit trail API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEventSummary(BaseModel):
    """Concise row for the investigation table."""

    event_id: uuid.UUID
    occurred_at: datetime
    category: str
    action: str
    action_label: str
    result: str
    summary: str
    actor_type: str
    actor_user_id: int | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    session_id: uuid.UUID | None = None
    client_ip: str | None = None
    client_label: str | None = None
    client_device_id: str | None = None
    account_id: int | None = None
    ibkr_account: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    provenance: str


class EmptySearchFilterHint(BaseModel):
    """How many events one active filter matches on its own."""

    field: str
    label: str
    value: str
    matches: int


class AuditEventsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AuditEventSummary]
    empty_filter_hints: list[EmptySearchFilterHint] | None = None
    """Present only when the search matched nothing and filters were active.

    Lists each active filter with the number of events it matches in isolation,
    so the operator can see which criterion excluded everything instead of
    removing filters one at a time.
    """


class FieldChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class AuthSessionInfo(BaseModel):
    session_id: uuid.UUID
    user_id: int
    user_email: str
    user_role: str
    auth_method: str
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None = None
    end_reason: str | None = None
    login_ip: str | None = None
    login_user_agent: str | None = None
    login_device_id: str | None = None
    login_audit_event_id: uuid.UUID | None = None


class InvestigationContext(BaseModel):
    """Read-time facts that help assess possible credential/session misuse.

    These are observations, not verdicts: each can have a legitimate cause.
    """

    session: AuthSessionInfo | None = None
    session_event_count: int | None = None
    concurrent_sessions: list[AuthSessionInfo] = []
    device_first_seen_at: datetime | None = None
    actor_ips_within_24h: list[dict[str, Any]] = []


class AuditEventDetail(AuditEventSummary):
    recorded_at: datetime
    completed_at: datetime | None = None
    result_reason: str | None = None
    auth_method: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    http_method: str | None = None
    http_path: str | None = None
    parameters: dict[str, Any] = {}
    before_state: Any = None
    after_state: Any = None
    changes: list[FieldChange] | None = None
    related: dict[str, Any] = {}
    ref_ids: list[str] = []
    context: dict[str, Any] = {}
    legacy_ref: str | None = None
    investigation: InvestigationContext


class AuditActionFacet(BaseModel):
    action: str
    label: str


class AuditCategoryFacet(BaseModel):
    category: str
    label: str
    actions: list[AuditActionFacet]


class AuditFacetsResponse(BaseModel):
    categories: list[AuditCategoryFacet]
    results: list[str]
    actor_types: list[str]
    actors: list[str]
    roles: list[str]
    browsers: list[str] = []
    target_types: list[str]
