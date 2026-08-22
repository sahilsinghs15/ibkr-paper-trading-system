"""Atomic acquire/promote/release for the execution dedupe barrier."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.execution_claim import (
    CLAIM_STATE_ABANDONED,
    CLAIM_STATE_CLAIMED,
    CLAIM_STATE_EXECUTED,
    ExecutionClaimModel,
)
from app.db.models.order import OrderModel
from app.db.models.signal import SignalModel

logger = logging.getLogger(__name__)


class DuplicateExecutionError(Exception):
    """The intent was already executed. Never retry."""


class ExecutionInFlightError(Exception):
    """Another worker or process currently holds the claim."""


class ClaimNeedsReconciliationError(Exception):
    """A prior attempt died holding the claim; broker state is unverified."""


class ExecutionClaimRepository:
    """Owns the execution_claims barrier."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(
        self,
        *,
        dedupe_key: str,
        account_id: int | None,
        strategy_id: str,
        signal_id: str,
        action: str,
        correlation_id: str | None = None,
        stale_after_sec: float = 300.0,
    ) -> ExecutionClaimModel:
        """Claim the right to execute this intent, or raise.

        Insert-or-retake in one statement: the ON CONFLICT arm only fires for a
        previously ABANDONED claim, so EXECUTED and live CLAIMED rows are left
        untouched and fall through to the diagnosis below.
        """
        now = datetime.now(UTC)
        stmt = (
            insert(ExecutionClaimModel)
            .values(
                dedupe_key=dedupe_key,
                account_id=account_id,
                strategy_id=strategy_id,
                signal_id=signal_id,
                action=action,
                state=CLAIM_STATE_CLAIMED,
                attempt_count=1,
                correlation_id=correlation_id,
                claimed_at=now,
            )
            .on_conflict_do_update(
                index_elements=["dedupe_key"],
                set_={
                    "state": CLAIM_STATE_CLAIMED,
                    "claimed_at": now,
                    "attempt_count": ExecutionClaimModel.attempt_count + 1,
                    "correlation_id": correlation_id,
                    "last_note": None,
                },
                where=ExecutionClaimModel.state == CLAIM_STATE_ABANDONED,
            )
            .returning(ExecutionClaimModel)
        )
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        if row is not None:
            return row

        existing = await self.get(dedupe_key)
        if existing is None:
            raise RuntimeError(f"Claim conflict but no row found for {dedupe_key}")

        if existing.state == CLAIM_STATE_EXECUTED:
            raise DuplicateExecutionError(
                f"DUPLICATE_EXECUTION: '{signal_id}' already executed at "
                f"{existing.executed_at} (attempt {existing.attempt_count})."
            )

        age = (now - existing.claimed_at).total_seconds()
        if age < stale_after_sec:
            raise ExecutionInFlightError(
                f"EXECUTION_IN_FLIGHT: '{signal_id}' claimed {age:.0f}s ago by "
                f"correlation_id={existing.correlation_id}."
            )

        raise ClaimNeedsReconciliationError(
            f"CLAIM_STALE: '{signal_id}' held since {existing.claimed_at} "
            f"({age:.0f}s); broker state unverified, refusing to re-execute."
        )

    async def get(self, dedupe_key: str) -> ExecutionClaimModel | None:
        res = await self._session.execute(
            select(ExecutionClaimModel).where(ExecutionClaimModel.dedupe_key == dedupe_key)
        )
        return res.scalar_one_or_none()

    async def mark_executed(self, dedupe_key: str, *, note: str | None = None) -> bool:
        """Promote a held claim to the permanent duplicate barrier."""
        stmt = (
            update(ExecutionClaimModel)
            .where(
                ExecutionClaimModel.dedupe_key == dedupe_key,
                ExecutionClaimModel.state == CLAIM_STATE_CLAIMED,
            )
            .values(
                state=CLAIM_STATE_EXECUTED,
                executed_at=datetime.now(UTC),
                last_note=note,
            )
        )
        res = await self._session.execute(stmt)
        return bool(res.rowcount)

    async def release(self, dedupe_key: str, *, note: str | None = None) -> bool:
        """Release a claim so the intent can be retried.

        Only call this when it is *known* that nothing reached the broker.
        """
        stmt = (
            update(ExecutionClaimModel)
            .where(
                ExecutionClaimModel.dedupe_key == dedupe_key,
                ExecutionClaimModel.state == CLAIM_STATE_CLAIMED,
            )
            .values(state=CLAIM_STATE_ABANDONED, last_note=note)
        )
        res = await self._session.execute(stmt)
        return bool(res.rowcount)

    async def count_orders_emitted(self, strategy_id: str, signal_id: str) -> int:
        """Orders already written for this (strategy_id, signal_id).

        ``orders.signal_id`` FKs to ``signals.id`` and ``signals.signal_id``
        carries the ``:CLOSE`` suffix, so OPEN and CLOSE are counted separately.
        """
        stmt = (
            select(func.count())
            .select_from(OrderModel)
            .join(SignalModel, OrderModel.signal_id == SignalModel.id)
            .where(
                SignalModel.strategy_id == strategy_id,
                SignalModel.signal_id == signal_id,
            )
        )
        res = await self._session.execute(stmt)
        return int(res.scalar_one() or 0)

    async def reconcile_stale_claims(self, stale_after_sec: float = 300.0) -> dict[str, int]:
        """Resolve claims left CLAIMED by a crashed attempt.

        No orders emitted -> the attempt never reached the broker, release it.
        Orders emitted -> promote to EXECUTED; the work was done and must not
        be repeated even though the process died before recording it.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_sec)
        res = await self._session.execute(
            select(ExecutionClaimModel).where(
                ExecutionClaimModel.state == CLAIM_STATE_CLAIMED,
                ExecutionClaimModel.claimed_at < cutoff,
            )
        )
        released = 0
        sealed = 0
        for claim in list(res.scalars().all()):
            emitted = await self.count_orders_emitted(claim.strategy_id, claim.signal_id)
            if emitted:
                await self.mark_executed(
                    claim.dedupe_key,
                    note=f"Sealed by reconciliation: {emitted} order(s) already emitted.",
                )
                sealed += 1
                logger.warning(
                    "Sealed stale execution claim %s (%d orders emitted)",
                    claim.dedupe_key,
                    emitted,
                )
            else:
                await self.release(
                    claim.dedupe_key, note="Released by reconciliation: no orders emitted."
                )
                released += 1
                logger.info("Released stale execution claim %s", claim.dedupe_key)
        return {"released": released, "sealed": sealed}


def execution_dedupe_key(intent: Any) -> str:
    """Stable barrier key for an OrderIntent.

    Mirrors ``duplicate_lookup_key`` but flattens to text so a single unique
    index works regardless of whether account_id is present (Postgres treats
    NULLs as distinct in composite unique constraints).
    """
    account = intent.account_id if intent.account_id is not None else "-"
    return f"{account}:{intent.strategy_id}:{intent.signal_id}"
