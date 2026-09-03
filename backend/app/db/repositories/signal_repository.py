"""Persistence for TradingView/external signals."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, literal, not_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.signal import (
    ACTIVE_LEASE_STATUSES,
    CLAIMABLE_STATUSES,
    JOB_STATUS_CLAIMED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_DEAD_LETTER,
    JOB_STATUS_FAILED,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RECOVERY_REQUIRED,
    JOB_STATUS_REJECTED,
    SignalJobModel,
    SignalModel,
)
from app.models.signal import Signal, SignalType

SIGNAL_STATUS_PROCESSED = "PROCESSED"
SIGNAL_STATUS_NEW = "NEW"
SIGNAL_STATUS_REJECTED = "REJECTED"


def persist_signal_id_for(signal: Signal) -> str:
    """Stable ``signals.signal_id``: OPEN uses trade_id; CLOSE uses ``{trade_id}:CLOSE``."""
    trade_id = (signal.trade_id or signal.signal_id or "").strip()
    action = str(signal.action or "").upper()
    if action == "CLOSE" and trade_id and not trade_id.endswith(":CLOSE"):
        return f"{trade_id}:CLOSE"
    return trade_id


def original_raw_payload(signal: Signal) -> dict[str, Any]:
    """Return a non-empty JSON object for ``signals.raw_payload``.

    Prefer the webhook capture envelope (``parsed_json`` / ``raw_body``). Never
    return ``{}``. If capture data is missing, reconstruct from the parsed Signal.
    """
    raw = signal.raw_payload
    if isinstance(raw, dict) and raw:
        parsed = raw.get("parsed_json")
        if isinstance(parsed, dict) and parsed:
            return raw
        if "raw_body" in raw or any(k in raw for k in ("strategy", "trade_id", "buckets", "action")):
            return raw
        return raw
    return _reconstruct_payload(signal)


def _reconstruct_payload(signal: Signal) -> dict[str, Any]:
    buckets = []
    for leg in signal.legs:
        buckets.append(
            {
                "underlying": leg.symbol,
                "legs": [
                    {
                        "instrument_type": leg.instrument_type,
                        "side": leg.payload_side,
                        "weight": leg.weight,
                        "price": str(leg.price),
                    }
                ],
            }
        )
    return {
        "strategy": signal.strategy_id,
        "action": str(signal.action or "").upper() or None,
        "trade_id": signal.trade_id or signal.signal_id,
        "direction": signal.direction,
        "market": signal.market,
        "buckets": buckets,
    }


def _audit_values(
    signal: Signal,
    *,
    persist_signal_id: str,
    status: str,
    reject_reason: str | None = None,
) -> dict[str, Any]:
    pair = ":".join(leg.symbol for leg in signal.legs) if signal.legs else ""
    if not pair and signal.trade_id:
        trade_parts = [p.split(":")[-1] for p in signal.trade_id.split("-") if ":" in p]
        if len(trade_parts) >= 2:
            pair = f"{trade_parts[0]}:{trade_parts[1]}"
        elif len(trade_parts) == 1:
            pair = trade_parts[0]
    if signal.direction is not None:
        side = str(signal.direction)
    elif signal.side:
        side = str(signal.side)
    elif signal.legs:
        sides = [str(leg.payload_side or "") for leg in signal.legs if leg.payload_side]
        side = ":".join(sides) if sides else "N/A"
    else:
        side = "N/A"
    price_a = signal.legs[0].price if signal.legs else (signal.price or Decimal(0))
    price_b = signal.legs[1].price if len(signal.legs) > 1 else None
    now = datetime.now(UTC)
    payload = original_raw_payload(signal)
    if not payload:
        raise ValueError("SIGNAL_PAYLOAD_EMPTY: refusing to persist raw_payload={}.")
    return {
        "strategy_id": signal.strategy_id or "",
        "signal_id": persist_signal_id,
        "trade_id": signal.trade_id or persist_signal_id.replace(":CLOSE", ""),
        "action": str(signal.action or "").upper(),
        "pair": pair or (signal.symbol or "N/A"),
        "side": side or "N/A",
        "ref_price_a": price_a,
        "ref_price_b": price_b,
        "raw_payload": payload,
        "status": status,
        "reject_reason": reject_reason,
        "processed_at": now if status == SIGNAL_STATUS_PROCESSED else None,
    }


class SignalRepository:
    """Signal inbox access. No sizing logic."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_strategy_signal(
        self, strategy_id: str, signal_id: str
    ) -> SignalModel | None:
        result = await self._session.execute(
            select(SignalModel).where(
                SignalModel.strategy_id == strategy_id,
                SignalModel.signal_id == signal_id,
            )
        )
        return result.scalar_one_or_none()

    async def is_processed(self, strategy_id: str, signal_id: str) -> bool:
        row = await self.get_by_strategy_signal(strategy_id, signal_id)
        return row is not None and row.status == SIGNAL_STATUS_PROCESSED

    async def list_processed_open_keys(self) -> set[tuple[str, str]]:
        result = await self._session.execute(
            select(SignalModel.strategy_id, SignalModel.signal_id).where(
                SignalModel.status == SIGNAL_STATUS_PROCESSED,
                SignalModel.action == "OPEN",
            )
        )
        return {(row[0], row[1]) for row in result.all()}

    async def record_inbound(
        self,
        signal: Signal,
        *,
        persist_signal_id: str | None = None,
        status: str = SIGNAL_STATUS_NEW,
        reject_reason: str | None = None,
    ) -> SignalModel:
        """Insert or fill in the audit row as soon as the webhook is parsed."""
        return await self._upsert(
            signal,
            persist_signal_id=persist_signal_id or persist_signal_id_for(signal),
            status=status,
            reject_reason=reject_reason,
        )

    async def record_processed(
        self,
        signal: Signal,
        *,
        persist_signal_id: str,
        status: str = SIGNAL_STATUS_PROCESSED,
    ) -> SignalModel:
        """Insert or update a processed signal row, filling any stub audit columns."""
        return await self._upsert(
            signal,
            persist_signal_id=persist_signal_id,
            status=status,
            reject_reason=None,
        )

    async def record_rejected_payload(
        self,
        payload: dict[str, Any],
        *,
        capture_data: dict[str, Any],
        reason: str,
    ) -> SignalModel:
        """Persist a parse/validation rejection with the original webhook body."""
        strategy_id = str(payload.get("strategy") or payload.get("strategy_id") or "").strip()
        trade_id = str(payload.get("trade_id") or payload.get("signal_id") or "").strip()
        action = str(payload.get("action") or "").strip().upper() or "UNKNOWN"
        persist_id = trade_id or str((capture_data.get("metadata") or {}).get("request_id") or "")
        if not persist_id:
            persist_id = f"REJECTED-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
        raw = capture_data if isinstance(capture_data, dict) and capture_data else {}
        if not raw.get("parsed_json"):
            raw = {**raw, "parsed_json": payload}
        signal = Signal(
            signal_type=SignalType.HOLD,
            timestamp=datetime.now(UTC),
            reason=reason,
            signal_id=persist_id,
            strategy_id=strategy_id or "unknown",
            action=action,
            trade_id=trade_id or persist_id,
            raw_payload=raw,
        )
        return await self._upsert(
            signal,
            persist_signal_id=persist_id,
            status=SIGNAL_STATUS_REJECTED,
            reject_reason=reason,
        )

    async def _upsert(
        self,
        signal: Signal,
        *,
        persist_signal_id: str,
        status: str,
        reject_reason: str | None,
    ) -> SignalModel:
        if not persist_signal_id:
            raise ValueError("SIGNAL_ID_REQUIRED: cannot persist a signal without signal_id/trade_id.")
        values = _audit_values(
            signal,
            persist_signal_id=persist_signal_id,
            status=status,
            reject_reason=reject_reason,
        )
        stmt = insert(SignalModel).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_signals_strategy_signal",
            set_={
                "trade_id": values["trade_id"],
                "action": values["action"],
                "pair": case(
                    (stmt.excluded.pair != "", stmt.excluded.pair),
                    else_=SignalModel.pair,
                ),
                "side": case(
                    (stmt.excluded.side != "N/A", stmt.excluded.side),
                    else_=SignalModel.side,
                ),
                "ref_price_a": stmt.excluded.ref_price_a,
                "ref_price_b": stmt.excluded.ref_price_b,
                "raw_payload": case(
                    (
                        SignalModel.raw_payload.op("?")(literal("parsed_json"))
                        & not_(stmt.excluded.raw_payload.op("?")(literal("parsed_json"))),
                        SignalModel.raw_payload,
                    ),
                    else_=stmt.excluded.raw_payload,
                ),
                "reject_reason": stmt.excluded.reject_reason,
                "status": case(
                    (
                        (stmt.excluded.status == SIGNAL_STATUS_PROCESSED)
                        | (stmt.excluded.status == SIGNAL_STATUS_REJECTED),
                        stmt.excluded.status,
                    ),
                    else_=SignalModel.status,
                ),
                "processed_at": case(
                    (SignalModel.processed_at.isnot(None), SignalModel.processed_at),
                    else_=stmt.excluded.processed_at,
                ),
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()
        row = await self.get_by_strategy_signal(signal.strategy_id or "", persist_signal_id)
        if row is None:
            raise RuntimeError(
                f"Failed to persist signal {persist_signal_id} for {signal.strategy_id}."
            )
        await self._session.refresh(row)
        return row


class SignalJobRepository:
    """Repository for managing signal jobs in the durable PostgreSQL queue."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(self, idempotency_key: str) -> SignalJobModel | None:
        stmt = select(SignalJobModel).where(SignalJobModel.idempotency_key == idempotency_key)
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def create_job_if_not_exists(
        self,
        *,
        signal_id: str,
        strategy_id: str,
        trade_id: str | None,
        idempotency_key: str,
        raw_payload: dict[str, Any],
        capture_data: dict[str, Any] | None,
        correlation_id: str,
        account_scope: str | None = None,
    ) -> tuple[SignalJobModel, bool]:
        """Atomically insert a job record if it does not already exist.

        Returns (job_model, created_boolean).
        """
        now = datetime.now(UTC)
        values = {
            "signal_id": signal_id,
            "strategy_id": strategy_id,
            "trade_id": trade_id,
            "status": JOB_STATUS_QUEUED,
            "idempotency_key": idempotency_key,
            "account_scope": account_scope,
            "raw_payload": raw_payload,
            "capture_data": capture_data,
            "correlation_id": correlation_id,
            "received_at": now,
            "queued_at": now,
        }
        stmt = (
            insert(SignalJobModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )
        res = await self._session.execute(stmt)
        if res.rowcount > 0:
            job = await self.get_by_idempotency_key(idempotency_key)
            assert job is not None
            return job, True
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is None:
            raise RuntimeError(f"Failed to fetch existing job for idempotency key {idempotency_key}")
        return existing, False

    def _sibling_in_flight_exists(self):
        sibling = aliased(SignalJobModel)
        return (
            select(literal(1))
            .select_from(sibling)
            .where(
                sibling.trade_id == SignalJobModel.trade_id,
                sibling.job_id != SignalJobModel.job_id,
                sibling.status.in_(ACTIVE_LEASE_STATUSES),
            )
            .exists()
        )

    def _claim_in_flight_exists(self):
        from app.db.models.execution_claim import CLAIM_STATE_CLAIMED, ExecutionClaimModel

        return (
            select(literal(1))
            .select_from(ExecutionClaimModel)
            .where(
                ExecutionClaimModel.strategy_id == SignalJobModel.strategy_id,
                ExecutionClaimModel.state == CLAIM_STATE_CLAIMED,
                (
                    (ExecutionClaimModel.signal_id == SignalJobModel.signal_id)
                    | (
                        (SignalJobModel.trade_id.is_not(None))
                        & (
                            (ExecutionClaimModel.signal_id == SignalJobModel.trade_id)
                            | (
                                ExecutionClaimModel.signal_id
                                == func.concat(SignalJobModel.trade_id, ":CLOSE")
                            )
                        )
                    )
                ),
            )
            .exists()
        )

    async def _trade_blocked_after_lock(self, job: SignalJobModel) -> bool:
        """True if a sibling lease or live CLAIMED barrier is visible after the advisory lock."""
        sibling = aliased(SignalJobModel)
        sibling_count = (
            await self._session.execute(
                select(func.count())
                .select_from(sibling)
                .where(
                    sibling.trade_id == job.trade_id,
                    sibling.job_id != job.job_id,
                    sibling.status.in_(ACTIVE_LEASE_STATUSES),
                )
            )
        ).scalar_one()
        if int(sibling_count or 0) > 0:
            return True
        from app.db.repositories.execution_claim_repository import ExecutionClaimRepository

        claim_repo = ExecutionClaimRepository(self._session)
        if await claim_repo.has_claimed(job.strategy_id, job.signal_id):
            return True
        if job.trade_id:
            if await claim_repo.has_claimed(job.strategy_id, job.trade_id):
                return True
            if await claim_repo.has_claimed(job.strategy_id, f"{job.trade_id}:CLOSE"):
                return True
        return False

    async def claim_next_jobs(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        lease_duration_sec: float = 30.0,
    ) -> list[SignalJobModel]:
        """Claim up to `limit` queued/expired-CLAIMED jobs using FOR UPDATE SKIP LOCKED.

        Expired PROCESSING is never retaken (M23) — the reclaimer quarantines it.
        Jobs sharing a ``trade_id`` take an advisory xact lock then re-check
        sibling leases so uncommitted CLAIMED rows serialize OPEN vs CLOSE (M18).
        """
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_duration_sec)
        claimed: list[SignalJobModel] = []
        skipped: list[Any] = []

        while len(claimed) < limit:
            candidate_id = await self._select_claim_candidate(now, skipped)
            if candidate_id is None:
                break
            job = await self._session.get(SignalJobModel, candidate_id)
            if job is None:
                skipped.append(candidate_id)
                continue
            if job.trade_id:
                await self._session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tid))"),
                    {"tid": str(job.trade_id)},
                )
                if await self._trade_blocked_after_lock(job):
                    skipped.append(candidate_id)
                    continue
            stmt = (
                update(SignalJobModel)
                .where(SignalJobModel.job_id == candidate_id)
                .values(
                    status=JOB_STATUS_CLAIMED,
                    worker_id=worker_id,
                    claimed_at=now,
                    lease_expires_at=lease_until,
                    attempt_count=SignalJobModel.attempt_count + 1,
                )
                .execution_options(synchronize_session="fetch")
            )
            await self._session.execute(stmt)
            await self._session.flush()
            await self._session.refresh(job)
            claimed.append(job)
        return claimed

    async def _select_claim_candidate(
        self, now: datetime, skipped: list[Any]
    ) -> Any | None:
        sibling_in_flight = self._sibling_in_flight_exists()
        claim_in_flight = self._claim_in_flight_exists()
        predicates = [
            (
                (SignalJobModel.status.in_(CLAIMABLE_STATUSES))
                | (
                    (SignalJobModel.status == JOB_STATUS_CLAIMED)
                    & (SignalJobModel.lease_expires_at < now)
                )
            ),
            (SignalJobModel.trade_id.is_(None) | not_(sibling_in_flight)),
            not_(claim_in_flight),
        ]
        if skipped:
            predicates.append(SignalJobModel.job_id.notin_(skipped))
        subq = (
            select(SignalJobModel.job_id)
            .where(*predicates)
            .order_by(SignalJobModel.received_at.asc())
            .with_for_update(skip_locked=True, of=SignalJobModel)
            .limit(1)
        )
        result = await self._session.execute(subq)
        return result.scalar_one_or_none()

    async def update_status(
        self,
        job_id: Any,
        status: str,
        *,
        error: str | None = None,
        worker_id: str | None = None,
        fence: bool = False,
        lease_duration_sec: float = 30.0,
    ) -> int:
        """Update job lifecycle status. Returns the number of rows affected.

        When ``fence`` is set the update only applies if ``worker_id`` still owns
        the job. A return value of 0 means the caller lost its lease (the job was
        reclaimed by another worker) and must not treat the write as applied.
        """
        now = datetime.now(UTC)
        values: dict[str, Any] = {"status": status}
        if status == JOB_STATUS_PROCESSING:
            values["processing_started_at"] = now
            # Extend the lease on the CLAIMED -> PROCESSING transition so there is
            # no unprotected window before the first heartbeat fires.
            values["lease_expires_at"] = now + timedelta(seconds=lease_duration_sec)
        elif status in (JOB_STATUS_COMPLETED, JOB_STATUS_REJECTED, JOB_STATUS_FAILED, JOB_STATUS_DEAD_LETTER):
            values["completed_at"] = now
            values["lease_expires_at"] = None
        if error is not None:
            values["last_error"] = error
        if worker_id is not None:
            values["worker_id"] = worker_id

        predicates = [SignalJobModel.job_id == job_id]
        if fence:
            if worker_id is None:
                raise ValueError("fence=True requires worker_id")
            predicates.append(SignalJobModel.worker_id == worker_id)
            predicates.append(SignalJobModel.status.in_(ACTIVE_LEASE_STATUSES))

        stmt = update(SignalJobModel).where(*predicates).values(**values)
        res = await self._session.execute(stmt)
        await self._session.flush()
        return int(res.rowcount or 0)

    async def heartbeat_lease(
        self, job_id: Any, worker_id: str, lease_duration_sec: float = 30.0
    ) -> bool:
        """Renew worker lease expiration timestamp.

        Returns False if the lease is no longer held by ``worker_id`` -- the job
        was reclaimed while this worker was still executing it.
        """
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_duration_sec)
        stmt = (
            update(SignalJobModel)
            .where(
                SignalJobModel.job_id == job_id,
                SignalJobModel.worker_id == worker_id,
                SignalJobModel.status.in_(ACTIVE_LEASE_STATUSES),
            )
            .values(lease_expires_at=lease_until)
        )
        res = await self._session.execute(stmt)
        return bool(res.rowcount)

    async def reclaim_stale_jobs(self, max_attempts: int = 3) -> dict[str, int]:
        """Reclaim jobs whose worker lease expired.

        A job that expired while still CLAIMED never began execution, so it is
        safe to requeue. A job that expired in PROCESSING may have already placed
        orders at the broker -- requeueing it blind would re-execute the signal,
        so it is quarantined as RECOVERY_REQUIRED for explicit reconciliation.
        Dead-letter only when there is no orders row and no live CLAIMED claim.
        """
        now = datetime.now(UTC)
        from app.db.repositories.execution_claim_repository import ExecutionClaimRepository

        claim_repo = ExecutionClaimRepository(self._session)
        expired_over_max = list(
            (
                await self._session.execute(
                    select(SignalJobModel).where(
                        SignalJobModel.status.in_(ACTIVE_LEASE_STATUSES),
                        SignalJobModel.lease_expires_at < now,
                        SignalJobModel.attempt_count >= max_attempts,
                    )
                )
            ).scalars().all()
        )
        dead_lettered = 0
        quarantined = 0
        for job in expired_over_max:
            emitted = await self.count_orders_emitted(job.strategy_id, job.signal_id)
            claimed = await claim_repo.has_claimed(job.strategy_id, job.signal_id)
            if emitted > 0 or claimed:
                job.status = JOB_STATUS_RECOVERY_REQUIRED
                job.last_error = (
                    f"Lease expired after {max_attempts} attempts with "
                    f"{emitted} order(s) emitted or CLAIMED barrier live; "
                    "requires reconciliation."
                )
                job.worker_id = None
                job.lease_expires_at = None
                quarantined += 1
            else:
                job.status = JOB_STATUS_DEAD_LETTER
                job.last_error = (
                    f"Exceeded max attempts ({max_attempts}) due to worker lease expiry."
                )
                job.completed_at = now
                job.worker_id = None
                job.lease_expires_at = None
                dead_lettered += 1

        stmt_quarantine = (
            update(SignalJobModel)
            .where(
                SignalJobModel.status == JOB_STATUS_PROCESSING,
                SignalJobModel.lease_expires_at < now,
                SignalJobModel.attempt_count < max_attempts,
            )
            .values(
                status=JOB_STATUS_RECOVERY_REQUIRED,
                last_error="Worker lease expired mid-execution; broker state unverified.",
                worker_id=None,
                lease_expires_at=None,
            )
        )
        res_quarantine = await self._session.execute(stmt_quarantine)
        quarantined += int(res_quarantine.rowcount or 0)

        stmt_requeue = (
            update(SignalJobModel)
            .where(
                SignalJobModel.status == JOB_STATUS_CLAIMED,
                SignalJobModel.lease_expires_at < now,
                SignalJobModel.attempt_count < max_attempts,
            )
            .values(
                status=JOB_STATUS_QUEUED,
                worker_id=None,
                lease_expires_at=None,
            )
        )
        res_requeue = await self._session.execute(stmt_requeue)
        await self._session.flush()
        return {
            "dead_lettered": dead_lettered,
            "quarantined": quarantined,
            "requeued": int(res_requeue.rowcount or 0),
        }

    async def count_orders_emitted(self, strategy_id: str, signal_id: str) -> int:
        """Count broker orders already written for a job's (strategy_id, signal_id).

        ``orders.signal_id`` is an FK to ``signals.id``, and ``signals.signal_id``
        carries the ``:CLOSE`` suffix, so this distinguishes the OPEN job from the
        CLOSE job on the same trade_id.
        """
        from app.db.models.order import OrderModel

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

