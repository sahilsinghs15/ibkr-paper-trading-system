"""Repository for the append-only operator audit trail.

Writes: ``insert`` and a single guarded ``finalize`` (PENDING -> terminal).
There is intentionally no update/delete API; the database enforces the same.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal

from sqlalchemy import String, and_, cast, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import INET, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditEventModel, AuthSessionModel

ORDER_TARGET_TYPES = ("MANUAL_ORDER", "ENGINE_ORDER")


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class AuditSearchFilters:
    date_from: datetime | None = None
    date_to: datetime | None = None
    actor: str | None = None
    actor_user_id: int | None = None
    actor_role: str | None = None
    client_ip: str | None = None  # single address or CIDR network
    categories: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    account: str | None = None  # numeric account_id or IBKR account code
    session_id: uuid.UUID | None = None
    device_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    ref_id: str | None = None
    order_id: str | None = None
    trade_id: str | None = None
    position_id: str | None = None
    correlation_id: str | None = None
    browser: str | None = None
    keyword: str | None = None
    provenance: Literal["all", "native", "legacy"] = "all"
    sort: Literal["newest", "oldest"] = "newest"
    limit: int = 50
    offset: int = 0


def _describe_filter_value(value: Any) -> str:
    """Render a filter value for an operator-facing hint."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def parse_ip_filter(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Parse an IP or CIDR filter; raises ValueError for invalid input."""
    return ipaddress.ip_network(value.strip(), strict=False)


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ writes

    async def insert(self, values: dict[str, Any]) -> None:
        await self._session.execute(insert(AuditEventModel).values(**values))

    async def finalize(
        self,
        event_id: uuid.UUID,
        *,
        result: str,
        result_reason: str | None,
        after_state: Any,
        related: dict[str, Any],
        ref_ids: list[str],
        target_id: str | None,
    ) -> bool:
        values: dict[str, Any] = {
            "result": result,
            "result_reason": result_reason,
            "after_state": after_state,
            "related": related,
            "ref_ids": ref_ids,
            "completed_at": func.clock_timestamp(),
        }
        if target_id is not None:
            values["target_id"] = func.coalesce(AuditEventModel.target_id, target_id)
        stmt = (
            update(AuditEventModel)
            .where(AuditEventModel.event_id == event_id, AuditEventModel.result == "PENDING")
            .values(**values)
        )
        res = await self._session.execute(stmt)
        return bool(getattr(res, "rowcount", 0))

    # ------------------------------------------------------------------ reads

    def _filters(self, f: AuditSearchFilters) -> list[Any]:
        m = AuditEventModel
        conds: list[Any] = []
        if f.date_from is not None:
            conds.append(m.occurred_at >= f.date_from)
        if f.date_to is not None:
            conds.append(m.occurred_at <= f.date_to)
        if f.actor:
            needle = f.actor.strip().lower()
            if "@" in needle and " " not in needle:
                actor_cond = func.lower(m.actor_email) == needle
                claimed_cond = func.lower(m.target_id) == needle
            else:
                pattern = f"%{_like_escape(needle)}%"
                actor_cond = m.actor_email.ilike(pattern, escape="\\")
                claimed_cond = m.target_id.ilike(pattern, escape="\\")
            # Failed logins carry the *claimed* identity as target; include them so
            # "everything involving alice's credentials" is one query.
            conds.append(or_(actor_cond, and_(m.actor_type == "ANONYMOUS", claimed_cond)))
        if f.actor_user_id is not None:
            conds.append(m.actor_user_id == f.actor_user_id)
        if f.actor_role:
            conds.append(m.actor_role == f.actor_role)
        if f.client_ip:
            network = parse_ip_filter(f.client_ip)
            conds.append(m.client_ip.op("<<=")(cast(literal(str(network)), INET)))
        if f.categories:
            conds.append(m.category.in_(f.categories))
        if f.actions:
            conds.append(m.action.in_(f.actions))
        if f.results:
            conds.append(m.result.in_(f.results))
        if f.account:
            acct = f.account.strip()
            if acct.isdigit():
                conds.append(m.account_id == int(acct))
            else:
                conds.append(func.upper(m.ibkr_account) == acct.upper())
        if f.session_id is not None:
            conds.append(m.session_id == f.session_id)
        if f.device_id:
            conds.append(m.client_device_id == f.device_id.strip())
        if f.target_type:
            conds.append(m.target_type == f.target_type)
        if f.target_id:
            conds.append(m.target_id == f.target_id.strip())
        if f.ref_id:
            conds.append(m.ref_ids.contains([f.ref_id.strip()]))
        if f.order_id:
            oid = f.order_id.strip()
            # Internal (ORD-/MAN_), broker order id and IBKR perm id are all in ref_ids.
            conds.append(
                or_(
                    m.ref_ids.contains([oid]),
                    and_(m.target_type.in_(ORDER_TARGET_TYPES), m.target_id == oid),
                )
            )
        if f.trade_id:
            tid = f.trade_id.strip()
            conds.append(
                or_(
                    m.ref_ids.contains([tid]),
                    and_(m.target_type == "POSITION", m.target_id == tid),
                )
            )
        if f.position_id:
            # Engine pairs and manual lots are identified by their trade id.
            pid = f.position_id.strip()
            conds.append(
                or_(
                    and_(m.target_type == "POSITION", m.target_id == pid),
                    m.ref_ids.contains([pid]),
                )
            )
        if f.correlation_id:
            cid = f.correlation_id.strip()
            conds.append(
                or_(
                    m.correlation_id == cid,
                    m.request_id == cid,
                    m.ref_ids.contains([cid]),  # e.g. kill-switch operation id
                )
            )
        if f.browser:
            conds.append(
                func.lower(m.context["client"]["browser"].astext) == f.browser.strip().lower()
            )
        if f.keyword:
            pattern = f"%{_like_escape(f.keyword.strip())}%"
            conds.append(
                or_(
                    m.summary.ilike(pattern, escape="\\"),
                    m.action.ilike(pattern, escape="\\"),
                    m.actor_email.ilike(pattern, escape="\\"),
                    m.target_id.ilike(pattern, escape="\\"),
                    m.ibkr_account.ilike(pattern, escape="\\"),
                    m.result_reason.ilike(pattern, escape="\\"),
                    func.array_to_string(m.ref_ids, " ").ilike(pattern, escape="\\"),
                    cast(m.parameters, String).ilike(pattern, escape="\\"),
                )
            )
        if f.provenance == "native":
            conds.append(m.provenance == "NATIVE")
        elif f.provenance == "legacy":
            conds.append(m.provenance != "NATIVE")
        return conds

    # Human labels for the diagnostic below; keyed by AuditSearchFilters field.
    _FILTER_LABELS: ClassVar[dict[str, str]] = {
        "date_from": "From date",
        "date_to": "To date",
        "actor": "User",
        "actor_user_id": "User id",
        "actor_role": "Role",
        "client_ip": "Source IP",
        "categories": "Category",
        "actions": "Action",
        "results": "Result",
        "account": "Account",
        "session_id": "Session",
        "device_id": "Device",
        "target_type": "Target type",
        "target_id": "Target id",
        "ref_id": "Reference id",
        "order_id": "Order id",
        "trade_id": "Trade id",
        "position_id": "Position id",
        "correlation_id": "Correlation id",
        "browser": "Browser",
        "keyword": "Keyword",
        "provenance": "Provenance",
    }

    def _active_filter_fields(self, f: AuditSearchFilters) -> list[str]:
        """Filter fields the caller actually set."""
        active: list[str] = []
        for name in self._FILTER_LABELS:
            value = getattr(f, name, None)
            if name == "provenance":
                if value != "all":
                    active.append(name)
            elif isinstance(value, list):
                if value:
                    active.append(name)
            elif value not in (None, ""):
                active.append(name)
        return active

    async def explain_empty(self, f: AuditSearchFilters) -> list[dict[str, Any]]:
        """For a search that matched nothing, count each active filter on its own.

        A zero-result search otherwise gives the operator no way to tell which
        criterion excluded everything, so the only recourse is to remove filters
        one at a time and search again. Reporting the per-filter counts turns
        that into a single answer. Only runs on the empty path, so the extra
        queries never affect a search that returned rows.
        """
        m = AuditEventModel
        out: list[dict[str, Any]] = []
        for name in self._active_filter_fields(f):
            single = replace(
                AuditSearchFilters(),
                **{name: getattr(f, name)},
            )
            conds = self._filters(single)
            stmt = select(func.count()).select_from(m)
            if conds:
                stmt = stmt.where(*conds)
            matches = (await self._session.execute(stmt)).scalar_one()
            out.append(
                {
                    "field": name,
                    "label": self._FILTER_LABELS[name],
                    "value": _describe_filter_value(getattr(f, name)),
                    "matches": int(matches),
                }
            )
        return out

    async def search(self, f: AuditSearchFilters) -> tuple[list[AuditEventModel], int]:
        m = AuditEventModel
        conds = self._filters(f)
        count_stmt = select(func.count()).select_from(m)
        stmt = select(m)
        if conds:
            count_stmt = count_stmt.where(*conds)
            stmt = stmt.where(*conds)
        if f.sort == "oldest":
            stmt = stmt.order_by(m.occurred_at.asc(), m.id.asc())
        else:
            stmt = stmt.order_by(m.occurred_at.desc(), m.id.desc())
        stmt = stmt.limit(f.limit).offset(f.offset)
        total = int((await self._session.execute(count_stmt)).scalar_one())
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get(self, event_id: uuid.UUID) -> AuditEventModel | None:
        stmt = select(AuditEventModel).where(AuditEventModel.event_id == event_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_auth_session(self, session_id: uuid.UUID) -> AuthSessionModel | None:
        return await self._session.get(AuthSessionModel, session_id)

    async def session_event_count(self, session_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(AuditEventModel).where(
            AuditEventModel.session_id == session_id
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def concurrent_sessions(
        self, user_id: int, at: datetime, exclude: uuid.UUID | None
    ) -> list[AuthSessionModel]:
        """Other sessions of the same user that were live at ``at``."""
        s = AuthSessionModel
        stmt = (
            select(s)
            .where(
                s.user_id == user_id,
                s.created_at <= at,
                s.expires_at > at,
                or_(s.ended_at.is_(None), s.ended_at > at),
            )
            .order_by(s.created_at.desc())
            .limit(20)
        )
        if exclude is not None:
            stmt = stmt.where(s.id != exclude)
        return list((await self._session.execute(stmt)).scalars().all())

    async def device_first_seen(self, user_id: int, device_id: str) -> datetime | None:
        stmt = select(func.min(AuditEventModel.occurred_at)).where(
            AuditEventModel.actor_user_id == user_id,
            AuditEventModel.client_device_id == device_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def recent_ips_for_user(
        self, user_id: int, at: datetime, window: timedelta
    ) -> list[tuple[str, int]]:
        m = AuditEventModel
        stmt = (
            select(cast(m.client_ip, String), func.count())
            .where(
                m.actor_user_id == user_id,
                m.client_ip.is_not(None),
                m.occurred_at >= at - window,
                m.occurred_at <= at + window,
            )
            .group_by(m.client_ip)
            .order_by(func.count().desc())
            .limit(20)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(str(ip).split("/")[0], int(n)) for ip, n in rows]

    async def distinct_values(self) -> dict[str, list[str]]:
        """Known actors / roles / target types for filter dropdowns (bounded)."""
        m = AuditEventModel
        actors = (
            await self._session.execute(
                select(m.actor_email)
                .where(m.actor_email.is_not(None))
                .group_by(m.actor_email)
                .order_by(func.max(m.occurred_at).desc())
                .limit(200)
            )
        ).scalars().all()
        roles = (
            await self._session.execute(
                select(m.actor_role).where(m.actor_role.is_not(None)).distinct().limit(20)
            )
        ).scalars().all()
        target_types = (
            await self._session.execute(
                select(m.target_type).where(m.target_type.is_not(None)).distinct().limit(50)
            )
        ).scalars().all()
        browsers = (
            await self._session.execute(
                select(m.context["client"]["browser"].astext)
                .where(m.context["client"]["browser"].astext.is_not(None))
                .distinct()
                .limit(30)
            )
        ).scalars().all()
        return {
            "browsers": sorted(b for b in browsers if b),
            "actors": [a for a in actors if a],
            "roles": sorted(r for r in roles if r),
            "target_types": sorted(t for t in target_types if t),
        }

    async def pending_older_than(self, age: timedelta) -> int:
        stmt = select(func.count()).select_from(AuditEventModel).where(
            AuditEventModel.result == "PENDING",
            AuditEventModel.occurred_at < datetime.now(UTC) - age,
        )
        return int((await self._session.execute(stmt)).scalar_one())
