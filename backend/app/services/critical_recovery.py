"""Automatic flatten-and-unlock for BASKET_CRITICAL incidents."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.positions import BrokerPositionLine
from app.db.models.account import AccountModel
from app.db.repositories.basket_repository import BasketRepository
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.order_repository import OrderRepository
from app.oms.basket import BasketState
from app.services.broker_flatten_service import BrokerFlattenService
from app.services.position_reconciler import QTY_EPSILON

logger = logging.getLogger(__name__)

POSITIONS_REQUEST_TIMEOUT_SEC = 15.0
RECOVERY_RETRY_DELAY_SEC = 30.0
MAX_RECOVERY_ATTEMPTS = 2
_FILL_EPS = 1e-8
RecoveryOutcome = Literal["done", "retry"]


@dataclass(frozen=True)
class LeftoverLeg:
    con_id: int
    symbol: str
    sec_type: str


def parse_ibkr_contract(ibkr_contract: str) -> tuple[str, str, str, str, int | None]:
    """Parse ``symbol-sec_type-exchange-currency[:con_id]`` identity keys."""
    con_id: int | None = None
    base = ibkr_contract.strip()
    if ":" in base:
        base, con_part = base.rsplit(":", 1)
        try:
            con_id = int(con_part)
        except ValueError:
            con_id = None
    parts = base.split("-")
    if len(parts) < 4:
        symbol = parts[0] if parts else base
        return symbol, "STK", "SMART", "USD", con_id
    symbol = parts[0]
    sec_type = parts[1]
    exchange = parts[2]
    currency = parts[3]
    return symbol, sec_type, exchange, currency, con_id


def _norm_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _norm_sec_type(sec_type: str) -> str:
    return sec_type.strip().upper()


class CriticalRecoveryService:
    """Background recovery: snapshot → flatten leftover conIds → verify flat → clear latch."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: Any,
        order_manager: Any | None = None,
        coordinator: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._order_manager = order_manager
        self._coordinator = coordinator
        self._in_flight: dict[tuple[int, str, str], asyncio.Task[None]] = {}
        self._attempts: dict[tuple[int, str, str], int] = {}

    def set_coordinator(self, coordinator: Any) -> None:
        self._coordinator = coordinator

    async def stop(self) -> None:
        """Cancel in-flight recovery tasks during application shutdown."""
        tasks = list(self._in_flight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._in_flight.clear()

    def schedule_recovery(
        self,
        *,
        account_id: int,
        trade_id: str,
        action: str,
        strategy_id: str,
    ) -> None:
        key = (account_id, trade_id, action)
        if key in self._in_flight:
            logger.debug(
                "Critical recovery already in flight account_id=%s trade_id=%s action=%s",
                account_id,
                trade_id,
                action,
            )
            return
        task = asyncio.create_task(
            self._run_recovery(
                account_id=account_id,
                trade_id=trade_id,
                action=action,
                strategy_id=strategy_id,
            )
        )
        self._in_flight[key] = task

        def _done(t: asyncio.Task[None]) -> None:
            self._in_flight.pop(key, None)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.exception(
                        "Critical recovery task failed account_id=%s trade_id=%s",
                        account_id,
                        trade_id,
                    )

        task.add_done_callback(_done)

    async def enqueue_all_critical(self) -> None:
        async with self._session_factory() as session, session.begin():
            repo = BasketRepository(session)
            rows = await repo.list_critical()
            for row in rows:
                if row.recovery_status == "RECOVERING":
                    await repo.update_recovery(
                        account_id=row.account_id,
                        trade_id=row.trade_id,
                        action=row.action,
                        recovery_detail=(
                            "Stale RECOVERING from prior process; re-enqueueing recovery."
                        ),
                    )
        for row in rows:
            self.schedule_recovery(
                account_id=row.account_id,
                trade_id=row.trade_id,
                action=row.action,
                strategy_id=row.strategy_id,
            )

    async def _run_recovery(
        self,
        *,
        account_id: int,
        trade_id: str,
        action: str,
        strategy_id: str,
    ) -> None:
        key = (account_id, trade_id, action)
        try:
            for attempt in range(1, MAX_RECOVERY_ATTEMPTS + 1):
                self._attempts[key] = attempt
                try:
                    outcome = await self._recover_once(
                        account_id=account_id,
                        trade_id=trade_id,
                        action=action,
                        strategy_id=strategy_id,
                        attempt=attempt,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Critical recovery attempt failed account_id=%s trade_id=%s attempt=%d",
                        account_id,
                        trade_id,
                        attempt,
                    )
                    outcome = "retry"
                if outcome == "done" or attempt >= MAX_RECOVERY_ATTEMPTS:
                    break
                await asyncio.sleep(RECOVERY_RETRY_DELAY_SEC)
        finally:
            self._attempts.pop(key, None)

    async def _recover_once(
        self,
        *,
        account_id: int,
        trade_id: str,
        action: str,
        strategy_id: str,
        attempt: int,
    ) -> RecoveryOutcome:
        async with self._session_factory() as session, session.begin():
            basket_repo = BasketRepository(session)
            row = await basket_repo.get(
                account_id=account_id, trade_id=trade_id, action=action
            )
            if row is None or row.state != BasketState.CRITICAL.value:
                return "done"
            account = (
                await session.execute(
                    select(AccountModel).where(AccountModel.id == account_id)
                )
            ).scalar_one_or_none()
            if account is None:
                return "done"
            ibkr_account = account.ibkr_account
            await basket_repo.update_recovery(
                account_id=account_id,
                trade_id=trade_id,
                action=action,
                recovery_status="RECOVERING",
                recovery_detail=f"Recovery attempt {attempt} started.",
            )
            basket_id = row.id

        leftovers = await self._collect_leftover_legs(basket_id)
        if not leftovers:
            detail = "No filled non-compensation legs; clearing latch."
            await self._clear_if_possible(
                account_id=account_id,
                strategy_id=strategy_id,
                trade_id=trade_id,
                action=action,
                recovery_detail=detail,
            )
            return "done"

        if not await self._fetch_and_persist_snapshot():
            await self._mark_failed(
                account_id=account_id,
                trade_id=trade_id,
                action=action,
                strategy_id=strategy_id,
                recovery_detail="Pre-flatten broker snapshot failed.",
                attempt=attempt,
            )
            return "retry" if attempt < MAX_RECOVERY_ATTEMPTS else "done"

        flatten_messages = await self._flatten_leftovers(
            ibkr_account=ibkr_account,
            leftovers=leftovers,
        )
        if not await self._fetch_and_persist_snapshot():
            await self._mark_failed(
                account_id=account_id,
                trade_id=trade_id,
                action=action,
                strategy_id=strategy_id,
                recovery_detail="Post-flatten broker snapshot failed.",
                attempt=attempt,
            )
            return "retry" if attempt < MAX_RECOVERY_ATTEMPTS else "done"

        if await self._broker_flat_for_conids(ibkr_account, leftovers):
            detail = "; ".join(flatten_messages) if flatten_messages else "Broker flat."
            await self._clear_if_possible(
                account_id=account_id,
                strategy_id=strategy_id,
                trade_id=trade_id,
                action=action,
                recovery_detail=detail,
            )
            return "done"

        detail = "; ".join(flatten_messages) if flatten_messages else "Broker still has qty."
        await self._mark_failed(
            account_id=account_id,
            trade_id=trade_id,
            action=action,
            strategy_id=strategy_id,
            recovery_detail=detail,
            attempt=attempt,
        )
        return "retry" if attempt < MAX_RECOVERY_ATTEMPTS else "done"

    async def _collect_leftover_legs(self, basket_id: int) -> list[LeftoverLeg]:
        async with self._session_factory() as session:
            orders = await OrderRepository(session).list_by_basket_id(basket_id)
        seen: set[int] = set()
        legs: list[LeftoverLeg] = []
        for order in orders:
            if order.is_compensation:
                continue
            fill_qty = float(order.fill_qty or 0)
            if fill_qty <= _FILL_EPS:
                continue
            symbol, sec_type, _exchange, _currency, con_id = parse_ibkr_contract(
                order.ibkr_contract
            )
            if con_id is None or con_id in seen:
                continue
            seen.add(con_id)
            legs.append(
                LeftoverLeg(
                    con_id=con_id,
                    symbol=_norm_symbol(symbol),
                    sec_type=_norm_sec_type(sec_type),
                )
            )
        return legs

    async def _fetch_and_persist_snapshot(self) -> bool:
        if not getattr(self._client, "is_connected", lambda: False)():
            logger.warning("Critical recovery snapshot skipped: TWS not connected")
            return False
        broker_lines: list[BrokerPositionLine] = []
        timed_out = False
        error: str | None = None
        try:
            request_async = getattr(self._client, "request_positions_async", None)
            if callable(request_async):
                broker_lines, timed_out = await request_async(
                    timeout=POSITIONS_REQUEST_TIMEOUT_SEC
                )
            else:
                error = "TWSClient.request_positions_async unavailable"
        except Exception as exc:
            logger.exception("Critical recovery failed to fetch IBKR positions")
            error = str(exc)

        if error is not None or timed_out:
            logger.warning(
                "Critical recovery snapshot incomplete timed_out=%s error=%s",
                timed_out,
                error,
            )
            return False

        finished_at = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            accounts = list(
                (await session.execute(select(AccountModel))).scalars().all()
            )
            ibkr_to_account = {acc.ibkr_account: acc.id for acc in accounts}
            snapshot_rows = [
                {
                    "ibkr_account": line.ibkr_account,
                    "con_id": line.con_id,
                    "account_id": ibkr_to_account.get(line.ibkr_account),
                    "symbol": _norm_symbol(line.symbol),
                    "sec_type": _norm_sec_type(line.sec_type),
                    "currency": line.currency,
                    "exchange": line.exchange,
                    "signed_qty": line.quantity,
                    "avg_cost": line.avg_cost,
                }
                for line in broker_lines
            ]
            await BrokerPositionRepository(session).replace_snapshot(
                snapshot_rows, as_of=finished_at
            )
        return True

    async def _flatten_leftovers(
        self,
        *,
        ibkr_account: str,
        leftovers: list[LeftoverLeg],
    ) -> list[str]:
        flatten_svc = BrokerFlattenService(
            session_factory=self._session_factory,
            order_manager=self._order_manager,
        )
        messages: list[str] = []
        for leg in leftovers:
            async with self._session_factory() as session:
                repo = BrokerPositionRepository(session)
                snap = await repo.get_snapshot_line(
                    ibkr_account=ibkr_account, con_id=leg.con_id
                )
            if snap is None:
                messages.append(f"con_id={leg.con_id}: no broker line (already flat).")
                continue
            if abs(float(snap.signed_qty)) <= QTY_EPSILON:
                messages.append(f"con_id={leg.con_id}: snapshot qty ~0.")
                continue
            try:
                result = await flatten_svc.flatten_line(
                    ibkr_account=ibkr_account,
                    symbol=leg.symbol,
                    sec_type=leg.sec_type,
                    con_id=leg.con_id,
                )
                messages.append(
                    f"con_id={leg.con_id}: {result.status} — {result.message}"
                )
            except Exception as exc:
                messages.append(f"con_id={leg.con_id}: flatten error — {exc}")
        return messages

    async def _broker_flat_for_conids(
        self, ibkr_account: str, leftovers: list[LeftoverLeg]
    ) -> bool:
        async with self._session_factory() as session:
            repo = BrokerPositionRepository(session)
            for leg in leftovers:
                snap = await repo.get_snapshot_line(
                    ibkr_account=ibkr_account, con_id=leg.con_id
                )
                if snap is None:
                    continue
                if abs(float(snap.signed_qty)) > QTY_EPSILON:
                    return False
        return True

    async def _clear_if_possible(
        self,
        *,
        account_id: int,
        strategy_id: str,
        trade_id: str,
        action: str,
        recovery_detail: str,
    ) -> None:
        if self._coordinator is None:
            logger.error("Critical recovery cannot clear latch: coordinator unavailable")
            return
        await self._coordinator.clear_critical(
            account_id=account_id,
            strategy_id=strategy_id,
            trade_id=trade_id,
            action=action,
            recovery_detail=recovery_detail,
        )

    async def _mark_failed(
        self,
        *,
        account_id: int,
        trade_id: str,
        action: str,
        strategy_id: str,
        recovery_detail: str,
        attempt: int,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await BasketRepository(session).update_recovery(
                account_id=account_id,
                trade_id=trade_id,
                action=action,
                recovery_status="FAILED",
                recovery_detail=recovery_detail,
            )
            await EventRepository(session).append(
                process="basket",
                kind="BASKET_CRITICAL_RECOVERY_FAILED",
                detail={
                    "account_id": account_id,
                    "trade_id": trade_id,
                    "strategy_id": strategy_id,
                    "action": action,
                    "attempt": attempt,
                    "recovery_detail": recovery_detail,
                },
                idempotency_key=(
                    f"basket_critical_recovery_failed:{account_id}:{trade_id}:{action}:{attempt}"
                ),
            )
        logger.error(
            "BASKET_CRITICAL_RECOVERY_FAILED: account_id=%s trade_id=%s attempt=%d detail=%s",
            account_id,
            trade_id,
            attempt,
            recovery_detail,
        )
