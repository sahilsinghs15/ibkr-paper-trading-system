"""Persistence of audit events with explicit durability semantics.

Three write modes, chosen per operation:

``record(session, entry)``
    Adds the audit row to the caller's open transaction. Used for single-
    transaction mutations (settings): the audit row commits atomically with the
    state change, or neither does.

``record_committed(entry)``
    Writes and commits the row in its own short transaction. Used for security
    events and for recording rejections after the business transaction rolled
    back. Best-effort: failures are logged, never raised.

``operation(entry)``
    Intent-first. The row is inserted as ``PENDING`` and **committed before**
    the side effect runs (broker orders, flattens, service lifecycle). When the
    block exits, the row is finalized exactly once (the database only allows a
    single PENDING -> terminal update). If the process dies mid-operation the
    committed PENDING row remains as durable evidence that the action was
    requested.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.audit.context import (
    AuditActor,
    RequestContext,
    build_request_context,
    session_observations,
)
from app.audit.sanitize import (
    clean_text,
    optional_text,
    sanitize_dict,
    sanitize_payload,
)
from app.audit.taxonomy import AuditAction, AuditResult, category_of
from app.db.repositories.audit_repository import AuditRepository

logger = logging.getLogger(__name__)


class AuditUnavailableError(HTTPException):
    """Raised when a required audit intent could not be made durable."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail="Audit trail unavailable; the operation was not executed.",
        )


def _ref_ids(related: dict[str, Any]) -> list[str]:
    """Flatten identifier values in ``related`` for GIN-indexed lookup."""
    out: list[str] = []

    def _add(value: Any) -> None:
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, list | tuple | set):
            for item in value:
                _add(item)
            return
        text = str(value).strip()
        if text and len(text) <= 128 and text not in out:
            out.append(text)

    for value in related.values():
        _add(value)
    return out[:200]


@dataclass
class AuditEntry:
    """Everything known about one operator action at the time it is recorded."""

    action: AuditAction
    actor: AuditActor
    summary: str
    request_ctx: RequestContext = field(default_factory=RequestContext)
    result: AuditResult = AuditResult.SUCCEEDED
    result_reason: str | None = None
    account_id: int | None = None
    ibkr_account: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    parameters: Any = None
    before_state: Any = None
    after_state: Any = None
    related: dict[str, Any] = field(default_factory=dict)
    extra_context: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        actor = self.actor
        ctx = self.request_ctx
        context: dict[str, Any] = ctx.context_dict()
        context["identity"] = {
            "actor_type": actor.actor_type.value,
            "service_label": actor.service_label,
            "token_issued_at": actor.token_issued_at.isoformat() if actor.token_issued_at else None,
            "session_created_at": actor.session.created_at.isoformat()
            if actor.session and actor.session.created_at
            else None,
            "note": (
                "Identity is the account the server authenticated for this request; "
                "it is not proof of which person operated the client."
            ),
        }
        observations = session_observations(actor, ctx)
        if observations is not None:
            context["session_observations"] = observations
        if self.extra_context:
            context["operation"] = self.extra_context
        related = sanitize_dict(self.related)
        return {
            "event_id": uuid.uuid4(),
            "category": category_of(self.action).value,
            "action": self.action.value,
            "result": self.result.value,
            "result_reason": optional_text(self.result_reason, 2000),
            "summary": clean_text(self.summary, max_len=500),
            "actor_type": actor.actor_type.value,
            "actor_user_id": actor.user_id,
            "actor_email": optional_text(actor.email, 255),
            "actor_role": optional_text(actor.role, 50),
            "session_id": actor.session.session_id if actor.session else None,
            "auth_method": optional_text(actor.auth_method, 32),
            "client_ip": ctx.client_ip,
            "user_agent": ctx.user_agent,
            "client_device_id": ctx.device_id,
            "request_id": ctx.request_id,
            "correlation_id": ctx.request_id,
            "http_method": ctx.http_method,
            "http_path": ctx.http_path,
            "account_id": self.account_id,
            "ibkr_account": optional_text(self.ibkr_account, 32),
            "target_type": optional_text(self.target_type, 48),
            "target_id": optional_text(self.target_id, 128),
            "parameters": sanitize_dict(self.parameters),
            "before_state": sanitize_payload(self.before_state)
            if self.before_state is not None
            else None,
            "after_state": sanitize_payload(self.after_state)
            if self.after_state is not None
            else None,
            "related": related,
            "ref_ids": _ref_ids(related),
            "context": sanitize_dict(context),
            "provenance": "NATIVE",
        }


class AuditOperation:
    """Handle for an in-flight intent-first audit record."""

    def __init__(
        self,
        recorder: AuditRecorder,
        event_id: uuid.UUID | None,
        related: dict[str, Any],
    ) -> None:
        self._recorder = recorder
        self.event_id = event_id
        self._related: dict[str, Any] = dict(related)
        self._outcome: tuple[AuditResult, str | None, Any, str | None] | None = None

    @property
    def durable(self) -> bool:
        return self.event_id is not None

    def add_related(self, **values: Any) -> None:
        for key, value in values.items():
            if value is not None:
                self._related[key] = value

    def set_outcome(
        self,
        result: AuditResult,
        *,
        reason: str | None = None,
        after: Any = None,
        target_id: str | None = None,
    ) -> None:
        self._outcome = (result, reason, after, target_id)

    async def _finalize(
        self,
        default_result: AuditResult,
        default_reason: str | None,
        *,
        from_exception: bool = False,
    ) -> None:
        if self.event_id is None:
            return
        after: Any = None
        target_id: str | None = None
        result, reason = default_result, default_reason
        if self._outcome is not None:
            outcome_result, outcome_reason, after, target_id = self._outcome
            # An exception raised after an outcome was set still wins.
            if not from_exception:
                result, reason = outcome_result, outcome_reason
        await self._recorder.finalize(
            self.event_id,
            result=result,
            reason=reason,
            after_state=after,
            related=self._related,
            target_id=target_id,
        )


def result_for_exception(exc: BaseException) -> tuple[AuditResult, str]:
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else repr(exc.detail)
        if exc.status_code in (401, 403):
            return AuditResult.DENIED, detail
        if 400 <= exc.status_code < 500:
            return AuditResult.REJECTED, detail
        return AuditResult.FAILED, detail
    if isinstance(exc, asyncio.CancelledError):
        return AuditResult.UNKNOWN, "Request was cancelled before the outcome was known."
    return AuditResult.FAILED, f"{type(exc).__name__}: {exc}"


class AuditRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, session: AsyncSession, entry: AuditEntry) -> uuid.UUID:
        """Append within the caller's transaction (commits with the caller)."""
        row = entry.to_row()
        await AuditRepository(session).insert(row)
        return row["event_id"]

    async def record_committed(self, entry: AuditEntry) -> uuid.UUID | None:
        """Append and commit in an independent transaction (best-effort)."""
        try:
            row = entry.to_row()
            async with self._session_factory() as session:
                await AuditRepository(session).insert(row)
                await session.commit()
            return row["event_id"]
        except Exception:
            logger.exception(
                "AUDIT WRITE FAILED action=%s actor=%s", entry.action.value, entry.actor.email
            )
            return None

    async def finalize(
        self,
        event_id: uuid.UUID,
        *,
        result: AuditResult,
        reason: str | None,
        after_state: Any,
        related: dict[str, Any],
        target_id: str | None,
    ) -> None:
        try:
            clean_related = sanitize_dict(related)
            async with self._session_factory() as session:
                updated = await AuditRepository(session).finalize(
                    event_id,
                    result=result.value,
                    result_reason=optional_text(reason, 2000),
                    after_state=sanitize_payload(after_state) if after_state is not None else None,
                    related=clean_related,
                    ref_ids=_ref_ids(clean_related),
                    target_id=optional_text(target_id, 128),
                )
                await session.commit()
            if not updated:
                logger.warning("Audit finalize skipped (already final?) event_id=%s", event_id)
        except Exception:
            logger.exception("AUDIT FINALIZE FAILED event_id=%s result=%s", event_id, result)

    @asynccontextmanager
    async def transaction(self, entry: AuditEntry) -> AsyncIterator[AuditTransaction]:
        """Audit a single-transaction mutation.

        The handler calls ``tx.commit(session, after=...)`` which writes the
        audit row inside the business transaction and commits both together.
        If the block raises before that commit, the refusal/failure is recorded
        in an independent transaction instead (the business change rolled back).
        """
        tx = AuditTransaction(self, entry)
        try:
            yield tx
        except GeneratorExit:
            raise
        except BaseException as exc:
            if not tx.committed:
                result, reason = result_for_exception(exc)
                entry.result = result
                entry.result_reason = reason
                await asyncio.shield(self.record_committed(entry))
            raise
        if not tx.committed:
            logger.warning("Audited transaction for %s exited without commit", entry.action.value)

    @asynccontextmanager
    async def operation(
        self,
        entry: AuditEntry,
        *,
        required: bool = True,
        success_result: AuditResult = AuditResult.SUCCEEDED,
    ) -> AsyncIterator[AuditOperation]:
        """Commit a PENDING intent before yielding, then finalize once.

        ``required=True`` fails closed (HTTP 503, operation not executed) if the
        intent cannot be made durable. Emergency risk-reducing actions pass
        ``required=False`` so an audit outage can never block a flatten.
        """
        entry.result = AuditResult.PENDING
        event_id = await self.record_committed(entry)
        if event_id is None:
            if required:
                raise AuditUnavailableError()
            logger.critical(
                "Proceeding WITHOUT durable audit intent for %s (non-blocking emergency action)",
                entry.action.value,
            )
        op = AuditOperation(self, event_id, entry.related)
        try:
            yield op
        except GeneratorExit:
            raise
        except BaseException as exc:
            result, reason = result_for_exception(exc)
            await asyncio.shield(op._finalize(result, reason, from_exception=True))
            raise
        await asyncio.shield(op._finalize(success_result, None))


class AuditTransaction:
    """Audit handle for a mutation committed in the caller's DB transaction."""

    def __init__(self, recorder: AuditRecorder, entry: AuditEntry) -> None:
        self._recorder = recorder
        self.entry = entry
        self.committed = False
        self.event_id: uuid.UUID | None = None

    async def commit(
        self,
        session: AsyncSession,
        *,
        after: Any = None,
        result: AuditResult = AuditResult.SUCCEEDED,
        result_reason: str | None = None,
        summary: str | None = None,
        account_id: int | None = None,
        ibkr_account: str | None = None,
        target_id: str | None = None,
        related: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Add the audit row to ``session`` and commit it with the mutation."""
        entry = self.entry
        entry.result = result
        entry.result_reason = result_reason
        if after is not None:
            entry.after_state = after
        if summary is not None:
            entry.summary = summary
        if account_id is not None:
            entry.account_id = account_id
        if ibkr_account is not None:
            entry.ibkr_account = ibkr_account
        if target_id is not None:
            entry.target_id = target_id
        if related:
            entry.related.update(related)
        self.event_id = await self._recorder.record(session, entry)
        await session.commit()
        self.committed = True
        return self.event_id


def get_audit_recorder(request: Request | None = None) -> AuditRecorder:
    factory = None
    if request is not None:
        factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from app.db.session import AsyncSessionLocal

        factory = AsyncSessionLocal
    return AuditRecorder(factory)


def audit_entry(
    request: Request | None,
    *,
    action: AuditAction,
    actor: AuditActor,
    summary: str,
    **fields: Any,
) -> AuditEntry:
    """Convenience constructor binding the observed request context."""
    return AuditEntry(
        action=action,
        actor=actor,
        summary=summary,
        request_ctx=build_request_context(request),
        **fields,
    )


def model_snapshot(obj: Any, *, exclude: Iterable[str] = ()) -> dict[str, Any] | None:
    """JSON-safe snapshot of an ORM row's column attributes."""
    if obj is None:
        return None
    from sqlalchemy import inspect as sa_inspect

    skip = set(exclude)
    mapper = sa_inspect(obj).mapper
    return sanitize_dict(
        {attr.key: getattr(obj, attr.key) for attr in mapper.column_attrs if attr.key not in skip}
    )


def values_equivalent(a: Any, b: Any) -> bool:
    """Equality that treats numerically-equal decimal strings as equal.

    Snapshots stringify Decimals, so ``"100000.0000"`` (DB scale) and
    ``"100000"`` (request value) must not be reported as a change.
    """
    if a == b:
        return True
    if isinstance(a, str | int | float) and isinstance(b, str | int | float):
        if isinstance(a, bool) or isinstance(b, bool):
            return False
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except (InvalidOperation, ValueError):
            return False
    return False


def changed_fields(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    """Keys whose (sanitized) values differ between two snapshots."""
    before = before or {}
    after = after or {}
    return sorted(
        k for k in set(before) | set(after) if not values_equivalent(before.get(k), after.get(k))
    )
