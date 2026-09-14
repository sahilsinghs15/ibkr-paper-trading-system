"""Background pair- and account-level risk-exit monitor.

Evaluates frozen pair stop/target/time_limit and live account daily risk
against session PnL. Pair exits call SinglePairCloseService; account
breaches arm the kill switch. Ships disabled (Settings + per-row flags).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.position_repository import PositionRepository
from app.services.kill_switch import (
    KillSwitchService,
    is_account_kill_switch_active,
)
from app.services.position_close_service import SinglePairCloseService
from app.services.risk_exit_rules import (
    REASON_PAIR_TIME_LIMIT,
    AccountRiskParams,
    ExitDecision,
    PairExitParams,
    evaluate_account_risk,
    evaluate_pair_exit,
    pair_entry_gross_notional,
)
from app.services.session_clock import SessionClock, get_session_clock
from app.services.trading_pause import is_account_trading_paused

logger = logging.getLogger(__name__)

REQUESTED_BY = "auto_risk"
PROCESS = "risk_exit"
KIND_PAIR_TRIGGERED = "PAIR_EXIT_TRIGGERED"
KIND_PAIR_FAILED = "PAIR_EXIT_FAILED"
KIND_ACCOUNT_BREACH = "ACCOUNT_RISK_BREACH"

ZERO = Decimal(0)


class RiskExitMonitor:
    """Periodic loop: evaluate open pairs and account session PnL, then exit."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        client: Any | None = None,
        live_pnl: Any | None = None,
        order_manager: Any | None = None,
        interval_sec: float = 2.0,
        max_pnl_staleness_sec: float = 15.0,
        max_retries: int = 3,
        enabled: bool = False,
        shadow_mode: bool = False,
        session_clock: SessionClock | None = None,
        pair_closer: SinglePairCloseService | None = None,
        kill_switch: KillSwitchService | None = None,
        now_fn: Any | None = None,
        monotonic_fn: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._live_pnl = live_pnl
        self._order_manager = order_manager
        self._interval_sec = float(interval_sec)
        self._max_pnl_staleness_sec = float(max_pnl_staleness_sec)
        self._max_retries = int(max_retries)
        self._enabled = bool(enabled)
        self._shadow_mode = bool(shadow_mode)
        self._clock = session_clock or get_session_clock()
        self._pair_closer = pair_closer or SinglePairCloseService(
            session_factory, order_manager
        )
        self._kill_switch = kill_switch or KillSwitchService(
            session_factory, order_manager
        )
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_fn or time.monotonic
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._tick_lock = asyncio.Lock()
        self._inflight_pairs: set[tuple[int, str]] = set()
        self._inflight_accounts: set[int] = set()
        self._pair_failures: dict[tuple[int, str], tuple[int, float]] = {}
        self._pair_exhausted: set[tuple[int, str]] = set()
        self._shadow_pair_emitted: set[tuple[int, str, str]] = set()
        self._shadow_account_emitted: set[tuple[int, str]] = set()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="risk-exit-monitor")
        logger.info(
            "RiskExitMonitor started (enabled=%s shadow=%s interval=%.1fs staleness=%.1fs)",
            self._enabled,
            self._shadow_mode,
            self._interval_sec,
            self._max_pnl_staleness_sec,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("RiskExitMonitor stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Risk-exit monitor tick failed")
            try:
                await asyncio.sleep(self._interval_sec)
            except asyncio.CancelledError:
                break

    def _in_rth(self, now: datetime) -> tuple[bool, datetime | None]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        from app.services.session_clock import ET

        now_et = now.astimezone(ET)
        day = now_et.date()
        open_dt = self._clock.rth_open(day)
        close_dt = self._clock.rth_close(day)
        if open_dt is None or close_dt is None:
            return False, None
        now_cmp = now.astimezone(open_dt.tzinfo)
        return open_dt <= now_cmp < close_dt, open_dt

    def _snapshot_fresh(self, snapshot: Any, now_mono: float) -> bool:
        if snapshot is None:
            return False
        if not getattr(snapshot, "all_legs_marked", False):
            return False
        updated = float(getattr(snapshot, "updated_at_mono", 0.0) or 0.0)
        if updated <= 0:
            return False
        return (now_mono - updated) <= self._max_pnl_staleness_sec

    def _pair_ready(self, key: tuple[int, str], now_mono: float) -> bool:
        if key in self._inflight_pairs or key in self._pair_exhausted:
            return False
        failure = self._pair_failures.get(key)
        return not (failure is not None and now_mono < failure[1])

    async def run_once(self) -> None:
        """Evaluate one tick. Skips if disabled, disconnected, or outside RTH."""
        if not self._enabled:
            return
        if self._tick_lock.locked():
            logger.debug("Risk-exit tick skipped: previous tick still running")
            return
        async with self._tick_lock:
            if self._client is not None and not getattr(
                self._client, "is_connected", lambda: False
            )():
                logger.debug("Risk-exit tick skipped: TWS disconnected")
                return
            now = self._now_fn()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            in_rth, session_open = self._in_rth(now)
            if not in_rth or session_open is None:
                return
            now_mono = self._monotonic()
            await self._evaluate(now=now, session_open=session_open, now_mono=now_mono)

    async def _evaluate(
        self,
        *,
        now: datetime,
        session_open: datetime,
        now_mono: float,
    ) -> None:
        async with self._session_factory() as session:
            pos_repo = PositionRepository(session)
            open_rows = await pos_repo.list_open()
            accounts = {
                row.id: row
                for row in (
                    await session.execute(select(AccountModel))
                ).scalars().all()
            }

        by_account: dict[int, list] = defaultdict(list)
        for row in open_rows:
            by_account[row.account_id].append(row)

        session_open_utc = session_open.astimezone(UTC)
        session_date = session_open.date().isoformat()
        breached_accounts: set[int] = set()

        account_ids = set(accounts) | set(by_account)
        for account_id in sorted(account_ids):
            account = accounts.get(account_id)
            if account is None:
                continue
            if is_account_kill_switch_active(account_id):
                continue
            if is_account_trading_paused(account_id):
                logger.info(
                    "TRADING_PAUSED: account_id=%s is paused; skipping daily-risk evaluation and square-off.",
                    account_id,
                )
                continue
            if account_id in self._inflight_accounts:
                continue
            if not getattr(account, "account_risk_enabled", False):
                continue
            positions = by_account.get(account_id, [])
            session_pnl = self._session_pnl(account_id, positions, now_mono)
            async with self._session_factory() as session:
                realised = await PositionRepository(session).sum_realised_closed_since(
                    account_id=account_id, since=session_open_utc
                )
            session_pnl = session_pnl + realised
            decision = evaluate_account_risk(
                AccountRiskParams(
                    daily_target=account.daily_target,
                    daily_stop=account.daily_stop,
                    daily_target_unit=getattr(account, "daily_target_unit", None)
                    or "ABSOLUTE",
                    daily_stop_unit=getattr(account, "daily_stop_unit", None)
                    or "ABSOLUTE",
                    total_margin=account.total_margin,
                ),
                session_pnl=session_pnl,
            )
            if decision is None:
                continue
            breached_accounts.add(account_id)
            await self._handle_account_breach(
                account_id=account_id,
                decision=decision,
                session_date=session_date,
            )

        for row in open_rows:
            if row.account_id in breached_accounts:
                continue
            if is_account_kill_switch_active(row.account_id):
                continue
            if not getattr(row, "exit_automation_enabled", False):
                continue
            key = (row.account_id, row.trade_id)
            if not self._pair_ready(key, now_mono):
                continue
            params = PairExitParams(
                target=row.target,
                stop=row.stop,
                time_limit=int(row.time_limit),
                target_unit=getattr(row, "target_unit", None) or "ABSOLUTE",
                stop_unit=getattr(row, "stop_unit", None) or "ABSOLUTE",
                opened_at=row.opened_at,
                entry_gross_notional=pair_entry_gross_notional(
                    leg_a_signed_qty=row.leg_a_signed_qty,
                    leg_a_entry_mark=row.leg_a_entry_mark,
                    leg_b_signed_qty=row.leg_b_signed_qty,
                    leg_b_entry_mark=row.leg_b_entry_mark,
                ),
            )
            snapshot = None
            if self._live_pnl is not None:
                getter = getattr(self._live_pnl, "get_pair_pnl", None)
                if callable(getter):
                    snapshot = getter(row.account_id, row.trade_id)
            fresh = self._snapshot_fresh(snapshot, now_mono)
            pnl = snapshot.pnl if snapshot is not None and fresh else ZERO  # pyrefly: ignore[missing-attribute]
            if not fresh:
                time_only = evaluate_pair_exit(params, pnl=pnl, now=now)
                if time_only is None or time_only.reason != REASON_PAIR_TIME_LIMIT:
                    logger.debug(
                        "Risk-exit skip pair account_id=%s trade_id=%s: stale PnL",
                        row.account_id,
                        row.trade_id,
                    )
                    continue
                decision = time_only
            else:
                decision = evaluate_pair_exit(params, pnl=pnl, now=now)
            if decision is None:
                continue
            await self._handle_pair_exit(row.account_id, row.trade_id, decision)

    def _session_pnl(
        self, account_id: int, positions: list, now_mono: float
    ) -> Decimal:
        """Sum unrealized PnL: fresh ticks when available, else positions.live_pnl."""
        total = ZERO
        if not positions:
            return total
        fallbacks: list[tuple[str, Decimal]] = []
        for pos in positions:
            snapshot = None
            if self._live_pnl is not None:
                getter = getattr(self._live_pnl, "get_pair_pnl", None)
                if callable(getter):
                    snapshot = getter(account_id, pos.trade_id)
            if self._snapshot_fresh(snapshot, now_mono):
                total += snapshot.pnl  # type: ignore[union-attr]
                continue
            live_pnl = getattr(pos, "live_pnl", None)
            pnl = ZERO if live_pnl is None else Decimal(str(live_pnl))
            total += pnl
            fallbacks.append((pos.trade_id, pnl))
        if fallbacks:
            logger.warning(
                "Risk-exit account_id=%s session PnL using persisted live_pnl "
                "fallbacks: %s",
                account_id,
                ", ".join(f"{trade_id}={pnl}" for trade_id, pnl in fallbacks),
            )
        return total

    async def _handle_account_breach(
        self,
        *,
        account_id: int,
        decision: ExitDecision,
        session_date: str,
    ) -> None:
        logger.warning(
            "ACCOUNT_RISK_BREACH account_id=%s reason=%s pnl=%s threshold=%s shadow=%s",
            account_id,
            decision.reason,
            decision.pnl,
            decision.threshold,
            self._shadow_mode,
        )
        await self._append_event(
            kind=KIND_ACCOUNT_BREACH,
            detail={
                "account_id": account_id,
                "reason": decision.reason,
                "pnl": str(decision.pnl),
                "threshold": str(decision.threshold),
                "shadow": self._shadow_mode,
            },
            idempotency_key=f"risk_exit:account:{account_id}:{session_date}:{decision.reason}",
        )
        if self._shadow_mode:
            self._shadow_account_emitted.add((account_id, session_date))
            return
        self._inflight_accounts.add(account_id)
        try:
            async with self._session_factory() as session, session.begin():
                await PositionRepository(session).set_exit_reason(
                    account_id=account_id, reason=decision.reason
                )
            op, created = await self._kill_switch.initiate_square_off(
                account_id, requested_by=REQUESTED_BY
            )
            if created:
                await self._kill_switch.execute_flatten_operation_background(
                    op.operation_id
                )
        except Exception:
            logger.exception(
                "Risk-exit kill switch failed account_id=%s reason=%s",
                account_id,
                decision.reason,
            )
        finally:
            self._inflight_accounts.discard(account_id)

    async def _handle_pair_exit(
        self, account_id: int, trade_id: str, decision: ExitDecision
    ) -> None:
        key = (account_id, trade_id)
        logger.warning(
            "PAIR_EXIT_TRIGGERED account_id=%s trade_id=%s reason=%s pnl=%s threshold=%s shadow=%s",
            account_id,
            trade_id,
            decision.reason,
            decision.pnl,
            decision.threshold,
            self._shadow_mode,
        )
        await self._append_event(
            kind=KIND_PAIR_TRIGGERED,
            detail={
                "account_id": account_id,
                "trade_id": trade_id,
                "reason": decision.reason,
                "pnl": str(decision.pnl),
                "threshold": str(decision.threshold),
                "shadow": self._shadow_mode,
            },
            idempotency_key=f"risk_exit:pair:{account_id}:{trade_id}:{decision.reason}",
        )
        if self._shadow_mode:
            self._shadow_pair_emitted.add((account_id, trade_id, decision.reason))
            return
        self._inflight_pairs.add(key)
        try:
            result = await self._pair_closer.close_pair(
                account_id, trade_id, exit_reason=decision.reason
            )
            success = bool(getattr(result, "success", False))
            if success:
                self._pair_failures.pop(key, None)
                self._pair_exhausted.discard(key)
                return
            await self._record_pair_failure(
                key, account_id, trade_id, decision, getattr(result, "status", "FAILED")
            )
        except Exception:
            logger.exception(
                "Risk-exit pair close raised account_id=%s trade_id=%s",
                account_id,
                trade_id,
            )
            await self._record_pair_failure(key, account_id, trade_id, decision, "ERROR")
        finally:
            self._inflight_pairs.discard(key)

    async def _record_pair_failure(
        self,
        key: tuple[int, str],
        account_id: int,
        trade_id: str,
        decision: ExitDecision,
        status: str,
    ) -> None:
        attempts, _ = self._pair_failures.get(key, (0, 0.0))
        attempts += 1
        if attempts >= self._max_retries:
            self._pair_exhausted.add(key)
            self._pair_failures.pop(key, None)
            await self._append_event(
                kind=KIND_PAIR_FAILED,
                detail={
                    "account_id": account_id,
                    "trade_id": trade_id,
                    "reason": decision.reason,
                    "status": status,
                    "attempts": attempts,
                },
                idempotency_key=f"risk_exit:pair_failed:{account_id}:{trade_id}",
            )
            logger.error(
                "PAIR_EXIT_FAILED account_id=%s trade_id=%s attempts=%s status=%s",
                account_id,
                trade_id,
                attempts,
                status,
            )
            return
        backoff = min(2 ** attempts, 60)
        self._pair_failures[key] = (attempts, self._monotonic() + backoff)
        logger.warning(
            "Risk-exit pair retry scheduled account_id=%s trade_id=%s attempt=%s backoff=%ss",
            account_id,
            trade_id,
            attempts,
            backoff,
        )

    async def _append_event(
        self,
        *,
        kind: str,
        detail: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await EventRepository(session).append(
                    process=PROCESS,
                    kind=kind,
                    detail=detail,
                    idempotency_key=idempotency_key,
                )
        except Exception:
            logger.exception("Risk-exit event append failed kind=%s", kind)
