"""OrderManager — application facade orchestrating Signal -> OrderIntent -> RMS -> OMS."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.config_service import AccountStrategyConfigService
from app.accounts.context import AccountExecutionContext
from app.accounts.router import DatabaseStrategyAccountRouter, StrategyAccountRouter
from app.core.config import get_settings
from app.core.logger import bind_log_context, get_log_context
from app.db.models.account import PerSymbolLimitModel
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.execution_claim_repository import (
    ExecutionClaimRepository,
    execution_dedupe_key,
)
from app.db.repositories.position_repository import PositionRepository
from app.db.repositories.signal_repository import (
    SIGNAL_STATUS_NEW,
    SIGNAL_STATUS_PROCESSED,
    SIGNAL_STATUS_REJECTED,
    SignalRepository,
    persist_signal_id_for,
)
from app.models.signal import Signal, SignalType
from app.oms.basket import BasketState
from app.oms.coordinator import BasketCoordinator
from app.oms.models import (
    AccountExecutionOutcome,
    ExecutionResult,
    FanoutExecutionResult,
    OMSOrder,
    executions_weighted_average,
)
from app.oms.oms_service import OMSService
from app.oms.retry_policy import (
    ExecutionRetryPolicy,
    default_paper_retry_policy,
    paper_retry_ports_allowed,
)
from app.rms.engine import RMSEngine
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    RMSContext,
    RMSOutcome,
    RMSResult,
    StrategyConfig,
    duplicate_lookup_key,
    exposure_key,
    open_position_key,
)
from app.rms.models import (
    OrderSide as RMSOrderSide,
)
from app.services.model_blue.allocation import CommittedCapitalProvider
from app.services.model_blue.parser import MODEL_BLUE_STRATEGY_ID
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.model_blue.sizer import ModelBlueSizer
from app.services.model_blue.strategy import ModelBlueStrategy
from app.services.model_blue.trade_book import ModelBlueTradeBook
from app.services.strategies.handler import StrategyHandler
from app.services.strategies.inbound import parse_tradingview_payload
from app.services.strategies.registry import StrategyRegistry

logger = logging.getLogger(__name__)

_STK_CONTRACT_MONTH = "2026-09"


def _row_pk(row) -> int | None:
    return None if row is None else int(row.id)


class OrderManager:
    """Application-level order execution facade.

    Translates strategy Signals into generic OrderIntents, evaluates RMS rules,
    and submits approved orders to the OMS.
    """

    def __init__(
        self,
        oms: OMSService | None = None,
        symbol: str | None = None,
        quantity: int | None = None,
        order_type: str = "MARKET",
        price: Decimal | None = None,
        strategy_id: str = "default_strategy",
        rms_engine: RMSEngine | None = None,
        rms_context: RMSContext | None = None,
        committed_capital_provider: CommittedCapitalProvider | None = None,
        model_blue_sizer: ModelBlueSizer | None = None,
        model_blue_trade_book: ModelBlueTradeBook | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        persistence: ModelBlueExecutionPersistence | None = None,
        strategy_registry: StrategyRegistry | None = None,
        account_router: StrategyAccountRouter | None = None,
    ) -> None:
        self._oms = oms
        self._symbol = symbol
        self._quantity = quantity
        self._order_type = order_type
        self._price = price
        self._strategy_id = strategy_id
        self._session_factory = session_factory
        self._persistence = persistence
        self._committed_capital_provider = committed_capital_provider
        self._account_router: StrategyAccountRouter | None = None
        if account_router is not None:
            self._account_router = account_router
        elif session_factory is not None:
            self._account_router = DatabaseStrategyAccountRouter(session_factory)
        else:
            self._account_router = None

        self._rms_engine = rms_engine or RMSEngine()
        self._rms_context = rms_context or RMSContext(
            strategy_configs={
                strategy_id: StrategyConfig(
                    strategy_id=strategy_id,
                    max_open_positions=100,
                    money_limit_per_symbol=Decimal(10_000_000),
                )
            }
        )
        self._ensure_strategy_config(MODEL_BLUE_STRATEGY_ID)
        self._ensure_strategy_config(strategy_id)

        self._model_blue_strategy = ModelBlueStrategy(
            committed_capital_provider=committed_capital_provider,
            sizer=model_blue_sizer,
            trade_book=model_blue_trade_book,
            persistence=persistence,
        )
        self._model_blue_sizer = self._model_blue_strategy._sizer
        self._model_blue_trades = self._model_blue_strategy._trades
        self.registry = strategy_registry or StrategyRegistry([self._model_blue_strategy])
        self._live_pnl = None
        self._baskets: BasketCoordinator | None = None
        self._exposure_locks: dict[object, asyncio.Lock] = {}
        self._exposure_locks_guard = asyncio.Lock()
        self._instrument_catalog = None
        if session_factory is not None:
            from app.db.repositories.instrument_repository import (
                DatabaseInstrumentCatalog,
            )

            self._instrument_catalog = DatabaseInstrumentCatalog(session_factory)
        if type(oms) is OMSService:
            self._baskets = BasketCoordinator(
                oms,
                session_factory=session_factory,
                fill_timeout=90.0,
                cancel_timeout=30.0,
                rms_engine=self._rms_engine,
                rms_context=self._rms_context,
                paper_retries_allowed=paper_retry_ports_allowed(get_settings().ibkr_port),
            )

    async def hydrate_runtime_from_db(self) -> None:
        """Load processed OPEN signals and account-scoped open-position counts."""
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            processed = await SignalRepository(session).list_processed_open_keys()
            self._rms_context.processed_signals.update(processed)
            if self._account_router is not None:
                by_strategy: dict[str, list[str]] = {}
                for strategy_id, signal_id in processed:
                    by_strategy.setdefault(strategy_id, []).append(signal_id)
                for strategy_id, signal_ids in by_strategy.items():
                    for ctx in await self._account_router.resolve(strategy_id):
                        for signal_id in signal_ids:
                            self._rms_context.processed_signals.add(
                                (ctx.account_id, strategy_id, signal_id)
                            )
            open_rows = await PositionRepository(session).list_open()
            for row in open_rows:
                pos_key = (row.account_id, row.strategy_id)
                self._rms_context.open_positions[pos_key] = (
                    self._rms_context.open_positions.get(pos_key, 0) + 1
                )
                self._rms_context.processed_signals.add(
                    (row.account_id, row.strategy_id, row.trade_id)
                )
                self._add_row_exposure(row)
            limits = (await session.execute(select(PerSymbolLimitModel))).scalars().all()
            self._apply_symbol_limits(limits)
        if self._baskets is not None:
            await self._baskets.hydrate_critical_from_db()
            await self._baskets.recover_incomplete_baskets()
        # Rebuild the blocked-account cache before any signal can be processed.
        from app.services.kill_switch import hydrate_kill_switch_cache

        await hydrate_kill_switch_cache(self._session_factory)
        await self.reload_execution_policy()
        critical_count = len(getattr(self._baskets, "_critical", set()) or set())
        logger.info(
            "Hydrated runtime from DB: processed_signals=%d open_position_keys=%d critical_baskets=%d",
            len(self._rms_context.processed_signals),
            len(self._rms_context.open_positions),
            critical_count,
        )

    def _apply_symbol_limits(self, limits: Sequence[PerSymbolLimitModel] | list) -> None:
        """Replace in-memory per-symbol money limits from DB rows."""
        self._rms_context.per_symbol_limits.clear()
        for limit in limits:
            self._rms_context.per_symbol_limits[(limit.account_id, limit.symbol)] = (
                limit.money_limit
            )

    async def reload_rms_limits(self) -> None:
        """Reload per-symbol money limits from Postgres into RMSContext."""
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            limits = (await session.execute(select(PerSymbolLimitModel))).scalars().all()
            self._apply_symbol_limits(limits)
        logger.info(
            "Reloaded RMS per_symbol_limits: count=%d",
            len(self._rms_context.per_symbol_limits),
        )

    async def reload_execution_policy(self) -> None:
        """Load paper square-off/retry settings from Postgres into the basket coordinator."""
        if self._baskets is None:
            return
        policy = default_paper_retry_policy()
        if self._session_factory is not None:
            try:
                async with self._session_factory() as session:
                    svc = AccountStrategyConfigService(session)
                    row = await svc.get_or_create_execution_settings()
                    await session.commit()
                    policy = ExecutionRetryPolicy(
                        enabled=row.enabled,
                        square_off_after_sec=float(row.square_off_after_sec),
                        max_retries=int(row.max_retries),
                        retry_interval_sec=float(row.retry_interval_sec),
                        retry_window_sec=float(row.retry_window_sec),
                    )
            except Exception:
                logger.exception(
                    "Failed to load execution_settings; using conservative in-memory defaults"
                )
        paper = paper_retry_ports_allowed(get_settings().ibkr_port)
        try:
            self._baskets.apply_retry_policy(policy, paper_retries_allowed=paper)
        except Exception:
            logger.exception("Failed to apply execution retry policy")

    async def hydrate_live_pnl(self) -> None:
        """Re-subscribe market data for OPEN positions after TWS is available.

        Does not submit orders. Backfills missing CFD master rows when TWS is up.
        """
        if self._live_pnl is None or self._session_factory is None:
            return
        async with self._session_factory() as session:
            open_rows = await PositionRepository(session).list_open()
        if not open_rows:
            return
        catalog = getattr(self, "_instrument_catalog", None)
        if catalog is not None:
            from app.instruments.cfd_discover import ensure_cfd_instruments_for_symbols
            from app.instruments.models import InstrumentResolutionError
            from app.instruments.resolver import ibkr_sec_type

            symbols: list[str] = []
            for position in open_rows:
                for symbol, raw_type in (
                    (position.leg_a_symbol, getattr(position, "leg_a_instrument_type", None)),
                    (position.leg_b_symbol, getattr(position, "leg_b_instrument_type", None)),
                ):
                    if not symbol:
                        continue
                    try:
                        if ibkr_sec_type(raw_type or "STK") == "CFD":
                            symbols.append(symbol)
                    except InstrumentResolutionError:
                        continue
            if symbols:
                await ensure_cfd_instruments_for_symbols(
                    symbols=symbols,
                    client=self._tws_client(),
                    session_factory=self._session_factory,
                    catalog=catalog,
                )
        snapshot = await self._instrument_snapshot_for_legs(
            [
                (row.leg_a_symbol, getattr(row, "leg_a_instrument_type", None))
                for row in open_rows
            ]
            + [
                (row.leg_b_symbol, getattr(row, "leg_b_instrument_type", None))
                for row in open_rows
                if row.leg_b_symbol
            ]
        )
        self._live_pnl.hydrate_from_position_rows(open_rows, catalog=snapshot)

    def _add_row_exposure(self, row) -> None:
        a_notional = abs(row.leg_a_signed_qty) * row.leg_a_entry_mark
        key_a = (row.account_id, row.leg_a_symbol)
        self._rms_context.symbol_exposures[key_a] = (
            self._rms_context.symbol_exposures.get(key_a, Decimal(0)) + a_notional
        )
        if row.leg_b_symbol and row.leg_b_signed_qty is not None and row.leg_b_entry_mark is not None:
            b_notional = abs(row.leg_b_signed_qty) * row.leg_b_entry_mark
            key_b = (row.account_id, row.leg_b_symbol)
            self._rms_context.symbol_exposures[key_b] = (
                self._rms_context.symbol_exposures.get(key_b, Decimal(0)) + b_notional
            )

    def parse_inbound_payload(
        self,
        payload: dict,
        *,
        timestamp: datetime,
        request_id: str,
        capture_data: dict,
    ) -> Signal:
        return parse_tradingview_payload(
            payload,
            timestamp=timestamp,
            request_id=request_id,
            capture_data=capture_data,
            registry=self.registry,
        )

    async def process_signal(self, signal: Signal) -> OMSOrder | None:
        """Process a trading signal through RMS evaluation and submit to OMS.

        Returns the first OMS order; callers that need every leg should use
        process_signal_execution().
        """
        result = await self.process_signal_execution(signal)
        if result is None:
            return None
        return result.order

    async def process_signal_execution(
        self, signal: Signal
    ) -> FanoutExecutionResult | ExecutionResult | None:
        """Process a signal: strategy -> eligible accounts -> per-account size/RMS/OMS."""
        if signal.signal_type == SignalType.HOLD:
            logger.info("HOLD signal received — no order submitted")
            return None

        bind_log_context(
            signal_id=signal.signal_id,
            trade_id=signal.trade_id or signal.signal_id,
        )
        inbound_row = await self._persist_inbound_signal(signal, status=SIGNAL_STATUS_NEW)
        try:
            res = await self._process_signal_execution_inner(signal, inbound_row)
            if res is not None and getattr(res, "all_rejected", False):
                reasons = []
                outcomes = getattr(res, "outcomes", [])
                for o in outcomes:
                    if getattr(o, "error", None):
                        reasons.append(f"Account {o.ibkr_account}: {o.error}")
                    elif getattr(o, "result", None) and getattr(o.result, "rms_result", None):
                        r_res = o.result.rms_result
                        if getattr(r_res, "reason", None):
                            reasons.append(f"Account {o.ibkr_account}: {r_res.reason}")
                rej_msg = "; ".join(reasons) if reasons else "Execution rejected by RMS/OMS policy"
                await self._persist_inbound_signal(
                    signal, status=SIGNAL_STATUS_REJECTED, reject_reason=rej_msg
                )
            return res
        except Exception as exc:
            await self._persist_inbound_signal(
                signal, status=SIGNAL_STATUS_REJECTED, reject_reason=str(exc)
            )
            raise

    async def _process_signal_execution_inner(
        self, signal: Signal, inbound_row
    ):
        strat_id = signal.strategy_id or self._strategy_id
        handler = self.registry.get(strat_id)
        if handler is None:
            if self._account_router is not None:
                raise ValueError(
                    f"UNKNOWN_STRATEGY: '{strat_id}' is not registered; "
                    "refusing IBKR submission without a strategy handler."
                )
            result = await self._process_legacy_single_name(signal, inbound_pk=_row_pk(inbound_row))
            return FanoutExecutionResult.from_single(result)

        if self._account_router is None:
            intent = await handler.build_intent(signal)
            result = await self._evaluate_and_submit(
                intent, signal, handler=handler, inbound_pk=_row_pk(inbound_row)
            )
            return FanoutExecutionResult.from_single(result)

        contexts = await self._account_router.resolve(strat_id)
        if not contexts:
            logger.warning(
                "NO_ELIGIBLE_ACCOUNTS: strategy '%s' has no enabled account subscriptions",
                strat_id,
            )
            raise ValueError(
                f"NO_ELIGIBLE_ACCOUNTS: strategy '{strat_id}' has no enabled "
                "account subscriptions."
            )
        return await self._fanout_accounts(
            signal, handler, contexts, inbound_pk=_row_pk(inbound_row)
        )

    async def _fanout_single_account(
        self,
        signal: Signal,
        handler: StrategyHandler,
        ctx: AccountExecutionContext,
        inbound_pk: int | None = None,
        raise_if_single: bool = False,
    ) -> AccountExecutionOutcome:
        bind_log_context(account_id=str(ctx.account_id))
        self._ensure_strategy_config(ctx.strategy_id)
        self._rms_context.account_open_limits[(ctx.account_id, ctx.strategy_id)] = (
            ctx.max_open_positions
        )
        try:
            intent = await handler.build_intent(signal, account=ctx)
            from app.services.kill_switch import is_account_kill_switch_active
            if intent.action == OrderAction.OPEN and is_account_kill_switch_active(ctx.account_id):
                logger.warning(
                    "KILL_SWITCH_ACTIVE: Blocking NEW open signal for account_id=%s ibkr=%s signal_id=%s",
                    ctx.account_id,
                    ctx.ibkr_account,
                    signal.signal_id,
                )
                raise ValueError(
                    f"KILL_SWITCH_ACTIVE: Account {ctx.account_id} is in active emergency kill-switch mode."
                )
            result = await self._evaluate_and_submit(
                intent, signal, handler=handler, inbound_pk=inbound_pk
            )
            logger.info(
                "Fanout account outcome: account_id=%s success=%s orders=%d",
                ctx.account_id,
                getattr(result, "success", None),
                len(result.orders) if result is not None else 0,
            )
            return AccountExecutionOutcome(
                account_id=ctx.account_id,
                ibkr_account=ctx.ibkr_account,
                result=result,
            )
        except ValueError as exc:
            logger.warning(
                "Account %s (%s) rejected signal %s: %s",
                ctx.account_id,
                ctx.ibkr_account,
                signal.signal_id,
                exc,
            )
            if raise_if_single:
                raise
            return AccountExecutionOutcome(
                account_id=ctx.account_id,
                ibkr_account=ctx.ibkr_account,
                result=None,
                error=str(exc),
            )

    async def _fanout_accounts(
        self,
        signal: Signal,
        handler: StrategyHandler,
        contexts: list[AccountExecutionContext],
        inbound_pk: int | None = None,
    ) -> FanoutExecutionResult:
        raise_if_single = len(contexts) == 1
        tasks = [
            self._fanout_single_account(
                signal, handler, ctx, inbound_pk=inbound_pk, raise_if_single=raise_if_single
            )
            for ctx in contexts
        ]
        outcomes = list(await asyncio.gather(*tasks))
        return FanoutExecutionResult(outcomes=outcomes)

    async def _process_legacy_single_name(
        self, signal: Signal, inbound_pk: int | None = None
    ) -> ExecutionResult:
        strat_id = signal.strategy_id or self._strategy_id
        sig_id = signal.signal_id or f"SIG-{uuid.uuid4().hex[:12].upper()}"
        target_symbol = signal.symbol or self._symbol
        target_price = signal.price if signal.price is not None else self._price

        if not target_symbol or target_symbol == "N/A":
            raise ValueError("MISSING_SYMBOL: Signal payload does not specify a valid symbol/ticker.")

        action_val = str(signal.action or "OPEN").upper()
        order_action = OrderAction.CLOSE if action_val == "CLOSE" else OrderAction.OPEN

        if signal.side:
            side_str = str(signal.side).upper()
            rms_side = RMSOrderSide.SELL if side_str in ("SELL", "SHORT") else RMSOrderSide.BUY
        else:
            rms_side = RMSOrderSide.BUY if signal.signal_type == SignalType.BUY else RMSOrderSide.SELL

        if order_action == OrderAction.OPEN:
            target_qty = signal.quantity if signal.quantity is not None else self._quantity
            if target_qty is None or target_qty <= 0:
                raise ValueError("MISSING_QUANTITY: Signal payload does not specify order quantity.")
        else:
            open_count = self._rms_context.open_positions.get(target_symbol, 0)
            if open_count <= 0:
                open_count = self._rms_context.open_positions.get(strat_id, 0)
            if open_count <= 0:
                raise ValueError(
                    f"NO_OPEN_POSITION: Cannot close position for '{target_symbol}': No active open position found in memory."
                )
            target_qty = signal.quantity if (signal.quantity is not None and signal.quantity > 0) else open_count

        if self._order_type.upper() == "LIMIT" and (target_price is None or target_price <= 0):
            raise ValueError(
                "MISSING_LIMIT_PRICE: Limit price is required and must be positive for LIMIT order type."
            )

        logger.info(
            "%s signal received — submitting order: symbol=%s qty=%s type=%s price=%s action=%s",
            signal.signal_type.value,
            target_symbol,
            str(target_qty),
            self._order_type,
            str(target_price),
            order_action.value,
        )

        intent = OrderIntent(
            signal_id=sig_id,
            strategy_id=strat_id,
            action=order_action,
            legs=[
                OrderLeg(
                    symbol=target_symbol,
                    side=rms_side,
                    quantity=target_qty,
                    price=target_price or Decimal(0),
                    contract_month=_STK_CONTRACT_MONTH,
                    instrument_type="STK",
                    leg_index=0,
                )
            ],
            timestamp=signal.timestamp or datetime.now(UTC),
        )
        return await self._evaluate_and_submit(
            intent, signal, handler=None, inbound_pk=inbound_pk
        )

    async def _acquire_execution_claim(self, intent: OrderIntent) -> str | None:
        """Take the durable dedupe barrier before any order reaches the broker.

        Commits in its own transaction: the barrier is worthless if it only
        becomes visible once the surrounding work succeeds.
        """
        if self._session_factory is None:
            return None
        dedupe_key = execution_dedupe_key(intent)
        action = intent.action.value if hasattr(intent.action, "value") else str(intent.action)
        async with self._session_factory() as session, session.begin():
            await ExecutionClaimRepository(session).acquire(
                dedupe_key=dedupe_key,
                account_id=intent.account_id,
                strategy_id=intent.strategy_id,
                signal_id=intent.signal_id,
                action=action,
                correlation_id=get_log_context().get("request_id"),
            )
        logger.info("Acquired execution claim %s", dedupe_key)
        return dedupe_key

    async def _seal_execution_claim(self, dedupe_key: str | None) -> None:
        """Promote a held claim to the permanent duplicate barrier."""
        if dedupe_key is None or self._session_factory is None:
            return
        try:
            async with self._session_factory() as session, session.begin():
                await ExecutionClaimRepository(session).mark_executed(dedupe_key)
        except Exception:
            logger.exception("Failed to seal execution claim %s", dedupe_key)

    async def _resolve_failed_claim(self, dedupe_key: str | None, intent: OrderIntent) -> None:
        """Decide a claim's fate after a failed submission.

        Release only when the order ledger proves nothing was emitted. If any
        order exists the outcome is unknown, so the claim stays CLAIMED and the
        reconciliation sweep resolves it against the ledger rather than letting
        a retry fire duplicate orders.
        """
        if dedupe_key is None or self._session_factory is None:
            return
        try:
            async with self._session_factory() as session, session.begin():
                repo = ExecutionClaimRepository(session)
                emitted = await repo.count_orders_emitted(
                    intent.strategy_id, intent.signal_id
                )
                if emitted:
                    logger.error(
                        "Execution claim %s held after failure: %d order(s) already "
                        "emitted; leaving for reconciliation.",
                        dedupe_key,
                        emitted,
                    )
                    return
                await repo.release(dedupe_key, note="Submission failed with no orders emitted.")
                logger.info("Released execution claim %s after clean failure", dedupe_key)
        except Exception:
            logger.exception("Failed to resolve execution claim %s", dedupe_key)

    async def _get_exposure_lock(self, key) -> asyncio.Lock:
        """Get or create the per-(account, symbol) exposure lock."""
        async with self._exposure_locks_guard:
            lock = self._exposure_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._exposure_locks[key] = lock
            return lock

    @asynccontextmanager
    async def _exposure_guard(self, intent: OrderIntent):
        """Serialize the read-modify-write on symbol_exposures for this intent.

        MoneyPerStockCheck reads context.symbol_exposures keyed on
        (account_id, symbol) -- a dimension that spans strategies -- but the
        write happens in _update_runtime_state, after the broker submit. The
        worker pool's domain lock is keyed on strategy, so two strategies
        trading the same symbol on one account both read a pre-submit budget
        and both pass. This holds every touched key from before RMS evaluation
        until after the exposure write.

        Keys are acquired in a stable sorted order so two intents with
        overlapping symbol sets cannot deadlock against each other.
        """
        keys = sorted({exposure_key(intent, leg.symbol) for leg in intent.legs}, key=repr)
        acquired: list[asyncio.Lock] = []
        try:
            for key in keys:
                lock = await self._get_exposure_lock(key)
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    async def _evaluate_and_submit(
        self,
        intent: OrderIntent,
        signal: Signal,
        *,
        handler: StrategyHandler | None,
        inbound_pk: int | None = None,
    ) -> ExecutionResult:
        """Evaluate and submit under the per-symbol exposure guard."""
        async with self._exposure_guard(intent):
            return await self._evaluate_and_submit_locked(
                intent, signal, handler=handler, inbound_pk=inbound_pk
            )

    async def _evaluate_and_submit_locked(
        self,
        intent: OrderIntent,
        signal: Signal,
        *,
        handler: StrategyHandler | None,
        inbound_pk: int | None = None,
    ) -> ExecutionResult:
        self._ensure_strategy_config(intent.strategy_id)

        for leg in intent.legs:
            if Decimal(str(leg.quantity)) <= 0:
                raise ValueError(
                    f"ZERO_QUANTITY: {leg.symbol} sized to {leg.quantity}; "
                    "refusing broker submission."
                )

        rms_result = self._rms_engine.evaluate(intent, self._rms_context)
        await self._audit_rms(intent, rms_result, signal_pk=inbound_pk)
        if rms_result.outcome != RMSOutcome.PASS:
            msg = f"RMS check {rms_result.check_number} rejected intent: {rms_result.reason}"
            logger.warning(msg)
            raise ValueError(msg)

        evaluated_intent = rms_result.intent
        evaluated_intent = await self._resolve_instruments(
            evaluated_intent, signal_pk=inbound_pk
        )

        if self._oms is None:
            raise RuntimeError("No OMSService configured on OrderManager.")

        if (
            intent.action == OrderAction.OPEN
            and self._baskets is not None
            and self._baskets.is_open_blocked(intent.account_id, intent.strategy_id)
        ):
            logger.warning(
                "BASKET_CRITICAL gate blocked OPEN: account_id=%s strategy_id=%s trade_id=%s",
                intent.account_id,
                intent.strategy_id,
                intent.signal_id,
            )
            raise ValueError(
                "BASKET_CRITICAL: new OPENs blocked for account_id="
                f"{intent.account_id} strategy={intent.strategy_id} until reconciliation."
            )

        use_leg_prices = handler is not None and handler.uses_per_leg_prices()

        # Barrier goes up here: after every gate has passed, immediately before
        # anything can reach the broker.
        dedupe_key = await self._acquire_execution_claim(evaluated_intent)

        if self._baskets is not None:
            try:
                basket_res = await self._baskets.execute(
                    evaluated_intent,
                    rms_result,
                    order_type=self._order_type,
                    signal_pk=inbound_pk,
                )
                child_orders = basket_res.orders or basket_res.compensation_orders
                if not child_orders:
                    raise RuntimeError("Basket coordinator returned no child orders.")
            except Exception:
                await self._resolve_failed_claim(dedupe_key, evaluated_intent)
                raise
            exec_res = ExecutionResult(
                order=child_orders[0],
                rms_result=rms_result,
                success=basket_res.success,
                error_message=None if basket_res.success else basket_res.state.value,
                orders=list(basket_res.orders) + list(basket_res.compensation_orders),
            )
            if basket_res.state in (BasketState.OPEN, BasketState.CLOSED):
                await self._seal_execution_claim(dedupe_key)
                filled_intent = self._intent_with_fills(evaluated_intent, basket_res.orders)
                await self._update_runtime_state(
                    filled_intent, exec_res, handler=handler, sized_from=signal
                )
                if self._live_pnl is not None and intent.account_id is not None:
                    trade_key = evaluated_intent.signal_id.split(":CLOSE")[0].split(":UNWIND:")[0]
                    if basket_res.state == BasketState.OPEN:
                        self._live_pnl.watch_open(filled_intent)
                    else:
                        self._live_pnl.unwatch(intent.account_id, trade_key)
            else:
                # Basket did not reach a settled state. Orders may be live at the
                # broker, so the claim stays held for reconciliation.
                self._record_unsettled_exposure(evaluated_intent, basket_res.orders)
                await self._resolve_failed_claim(dedupe_key, evaluated_intent)
            return exec_res

        try:
            exec_res = await self._oms.submit_intent(
                intent=evaluated_intent,
                rms_result=rms_result,
                limit_price=None if use_leg_prices else (signal.price if signal.price is not None else self._price),
                order_type=self._order_type,
            )
            if not exec_res.success:
                raise RuntimeError(f"OMS submission failed: {exec_res.error_message}")
        except Exception:
            await self._resolve_failed_claim(dedupe_key, evaluated_intent)
            raise

        await self._seal_execution_claim(dedupe_key)
        await self._update_runtime_state(evaluated_intent, exec_res, handler=handler, sized_from=signal)
        return exec_res

    async def _audit_rms(
        self, intent: OrderIntent, rms_result: RMSResult, *, signal_pk: int | None = None
    ) -> None:
        if self._session_factory is None:
            return
        detail = {
            "account_id": intent.account_id,
            "ibkr_account": intent.ibkr_account,
            "trade_id": intent.signal_id,
            "strategy_id": intent.strategy_id,
            "action": intent.action.value if hasattr(intent.action, "value") else str(intent.action),
            "outcome": rms_result.outcome.value,
            "reason": rms_result.reason,
            "check_number": rms_result.check_number,
            "checks": [
                {
                    "number": item.check_number,
                    "name": item.check_name,
                    "outcome": item.outcome.value,
                    "reason": item.reason,
                }
                for item in rms_result.check_results
            ],
        }
        try:
            async with self._session_factory() as session, session.begin():
                await EventRepository(session).append(
                    process="rms",
                    kind=f"RMS_{rms_result.outcome.value}",
                    detail=detail,
                    signal_id=signal_pk,
                    idempotency_key=(
                        f"rms_{rms_result.outcome.value.lower()}:{intent.account_id}:"
                        f"{intent.signal_id}"
                    ),
                )
        except Exception:
            logger.exception("Failed to persist RMS audit event")

    def _intent_with_fills(self, intent: OrderIntent, orders: list[OMSOrder]) -> OrderIntent:
        filled_legs = []
        for index, leg in enumerate(intent.legs):
            matching = [
                o
                for o in orders
                if o.leg_index == index and not getattr(o, "is_compensation", False)
            ]
            if not matching:
                filled_legs.append(leg)
                continue
            qty = sum(float(o.filled_quantity) for o in matching)
            px = leg.price
            for order in matching:
                execs = getattr(order, "executions", None) or {}
                derived = executions_weighted_average(execs) if execs else None
                if derived is not None:
                    px = derived
                    break
                cand = order.average_fill_price or order.last_fill_price
                if cand is not None:
                    px = cand
                    break
            if not isinstance(px, Decimal):
                px = Decimal(str(px))
            filled_legs.append(
                replace(
                    leg,
                    quantity=qty,
                    price=px,
                    notional=Decimal(str(qty)) * px,
                )
            )
        return replace(intent, legs=filled_legs)

    def _record_unsettled_exposure(
        self, intent: OrderIntent, orders: list[OMSOrder]
    ) -> None:
        """Book exposure for fills from a basket that never reached OPEN/CLOSED.

        _update_runtime_state only runs on a settled basket, so a CRITICAL or
        UNWINDING basket that partially filled left symbol_exposures reporting
        zero while real risk sat at the broker. is_open_blocked only gates the
        same (account_id, strategy_id) pair, but exposures are keyed on
        (account_id, symbol) -- so a different strategy on the same account
        would size against an understated budget.

        Deliberately does NOT touch processed_signals or open_positions: the
        trade is not cleanly open, and the execution claim stays held so the
        signal cannot be retried behind our back.
        """
        if not orders:
            return
        try:
            filled_intent = self._intent_with_fills(intent, orders)
        except Exception:
            logger.exception(
                "Could not compute fills for unsettled basket trade_id=%s; "
                "exposure NOT booked",
                intent.signal_id,
            )
            return

        opening = intent.action == OrderAction.OPEN
        booked: list[tuple[object, Decimal]] = []
        for leg in filled_intent.legs:
            if Decimal(str(leg.quantity)) <= 0:
                continue
            exp_key = exposure_key(intent, leg.symbol)
            current = self._rms_context.symbol_exposures.get(exp_key, Decimal(0))
            delta = leg.effective_notional
            updated = current + delta if opening else max(Decimal(0), current - delta)
            self._rms_context.symbol_exposures[exp_key] = updated
            booked.append((exp_key, delta))

        if booked:
            logger.warning(
                "UNSETTLED_EXPOSURE_BOOKED: trade_id=%s action=%s entries=%s",
                intent.signal_id,
                "OPEN" if opening else "CLOSE",
                [(str(k), str(v)) for k, v in booked],
            )

    async def _update_runtime_state(
        self,
        intent: OrderIntent,
        exec_res: ExecutionResult,
        *,
        handler: StrategyHandler | None,
        sized_from: Signal,
    ) -> None:
        if handler is not None:
            if intent.action == OrderAction.OPEN:
                self._rms_context.processed_signals.add(duplicate_lookup_key(intent))
                pos_key = open_position_key(intent)
                self._rms_context.open_positions[pos_key] = (
                    self._rms_context.open_positions.get(pos_key, 0) + 1
                )
                for leg in intent.legs:
                    exp_key = exposure_key(intent, leg.symbol)
                    self._rms_context.symbol_exposures[exp_key] = (
                        self._rms_context.symbol_exposures.get(exp_key, Decimal(0))
                        + leg.effective_notional
                    )
            elif intent.action == OrderAction.CLOSE:
                pos_key = open_position_key(intent)
                new_strat_qty = max(0, self._rms_context.open_positions.get(pos_key, 0) - 1)
                self._rms_context.open_positions[pos_key] = new_strat_qty
                for leg in intent.legs:
                    exp_key = exposure_key(intent, leg.symbol)
                    remaining = self._rms_context.symbol_exposures.get(exp_key, Decimal(0))
                    self._rms_context.symbol_exposures[exp_key] = max(
                        Decimal(0), remaining - leg.effective_notional
                    )
            await handler.after_submit(sized_from, intent, exec_res)
            await self._persist_inbound_signal(sized_from, status=SIGNAL_STATUS_PROCESSED)
            return

        target_symbol = intent.legs[0].symbol
        target_qty = int(intent.legs[0].quantity)
        strat_id = intent.strategy_id
        if intent.action == OrderAction.OPEN:
            self._rms_context.open_positions[target_symbol] = (
                self._rms_context.open_positions.get(target_symbol, 0) + target_qty
            )
            self._rms_context.open_positions[strat_id] = (
                self._rms_context.open_positions.get(strat_id, 0) + target_qty
            )
        elif intent.action == OrderAction.CLOSE:
            new_sym_qty = max(0, self._rms_context.open_positions.get(target_symbol, 0) - target_qty)
            new_strat_qty = max(0, self._rms_context.open_positions.get(strat_id, 0) - target_qty)
            self._rms_context.open_positions[target_symbol] = new_sym_qty
            self._rms_context.open_positions[strat_id] = new_strat_qty

        await self._persist_inbound_signal(sized_from, status=SIGNAL_STATUS_PROCESSED)

    async def record_rejected_inbound(
        self,
        payload: dict,
        *,
        capture_data: dict,
        reason: str,
    ) -> None:
        """Persist a webhook that failed strategy parse, with the original JSON."""
        if self._session_factory is None:
            return
        try:
            async with self._session_factory() as session, session.begin():
                row = await SignalRepository(session).record_rejected_payload(
                    payload, capture_data=capture_data, reason=reason
                )
                logger.info(
                    "Persisted rejected webhook signal_id=%s strategy_id=%s "
                    "raw_payload_keys=%s reason=%s",
                    row.signal_id,
                    row.strategy_id,
                    list((row.raw_payload or {}).keys()),
                    reason,
                )
        except Exception:
            logger.exception("Failed to persist rejected TradingView payload")

    async def _persist_inbound_signal(
        self,
        signal: Signal,
        *,
        status: str,
        reject_reason: str | None = None,
    ):
        if self._session_factory is None:
            return None
        persist_id = persist_signal_id_for(signal)
        if not persist_id:
            logger.warning("Cannot persist inbound signal without trade_id/signal_id")
            return None
        try:
            async with self._session_factory() as session, session.begin():
                row = await SignalRepository(session).record_inbound(
                    signal,
                    persist_signal_id=persist_id,
                    status=status,
                    reject_reason=reject_reason,
                )
                action = str(signal.action or "").upper()
                received_kind = "CLOSE_SIGNAL_RECEIVED" if action == "CLOSE" else "SIGNAL_RECEIVED"
                await EventRepository(session).append(
                    process="webhook",
                    kind=received_kind,
                    detail={
                        "trade_id": signal.trade_id or persist_id,
                        "strategy_id": signal.strategy_id,
                        "action": action,
                        "signal_id": persist_id,
                    },
                    signal_id=row.id,
                    idempotency_key=f"{received_kind.lower()}:{row.strategy_id}:{persist_id}",
                )
                await EventRepository(session).append(
                    process="webhook",
                    kind="SIGNAL_PERSISTED",
                    detail={
                        "trade_id": row.trade_id,
                        "strategy_id": row.strategy_id,
                        "status": row.status,
                        "pair": row.pair,
                        "signal_id": persist_id,
                    },
                    signal_id=row.id,
                    idempotency_key=f"signal_persisted:{row.strategy_id}:{persist_id}:{row.status}",
                )
                logger.info(
                    "Persisted inbound signal strategy_id=%s signal_id=%s trade_id=%s "
                    "pair=%s side=%s status=%s db_pk=%s reject_reason=%s "
                    "raw_payload_keys=%s has_parsed_json=%s",
                    row.strategy_id,
                    row.signal_id,
                    row.trade_id,
                    row.pair,
                    row.side,
                    row.status,
                    row.id,
                    reject_reason,
                    list((row.raw_payload or {}).keys()),
                    isinstance((row.raw_payload or {}).get("parsed_json"), dict),
                )
                return row
        except Exception:
            logger.exception(
                "Failed to persist inbound signal trade_id=%s", signal.trade_id
            )
            return None

    def _ensure_strategy_config(
        self, strategy_id: str, *, max_open_positions: int | None = None
    ) -> None:
        if not strategy_id:
            return
        existing = self._rms_context.strategy_configs.get(strategy_id)
        if existing is None:
            self._rms_context.strategy_configs[strategy_id] = StrategyConfig(
                strategy_id=strategy_id,
                max_open_positions=max_open_positions if max_open_positions is not None else 100,
                money_limit_per_symbol=Decimal(10_000_000),
            )
            return
        if max_open_positions is not None and existing.max_open_positions != max_open_positions:
            self._rms_context.strategy_configs[strategy_id] = StrategyConfig(
                strategy_id=strategy_id,
                max_open_positions=max_open_positions,
                money_limit_per_symbol=existing.money_limit_per_symbol,
            )

    async def _resolve_instruments(
        self, intent: OrderIntent, *, signal_pk: int | None = None
    ) -> OrderIntent:
        from app.instruments.cfd_discover import ensure_cfd_instruments_for_symbols
        from app.instruments.execution_override import execution_instrument_type
        from app.instruments.models import InstrumentResolutionError
        from app.instruments.resolver import attach_resolved, ibkr_sec_type

        catalog = getattr(self, "_instrument_catalog", None)
        if catalog is not None and self._session_factory is not None:
            cfd_symbols: list[str] = []
            for leg in intent.legs:
                try:
                    execution_type, _override = execution_instrument_type(
                        leg.instrument_type
                    )
                    if ibkr_sec_type(execution_type) != "CFD":
                        continue
                except InstrumentResolutionError:
                    continue
                cfd_symbols.append(leg.symbol)
            if cfd_symbols:
                await ensure_cfd_instruments_for_symbols(
                    symbols=cfd_symbols,
                    client=self._tws_client(),
                    session_factory=self._session_factory,
                    catalog=catalog,
                    market=intent.market,
                )

        snapshot = await self._instrument_snapshot_for_legs(
            [(leg.symbol, leg.instrument_type) for leg in intent.legs]
        )
        try:
            resolved_intent = attach_resolved(intent, catalog=snapshot)
        except InstrumentResolutionError as exc:
            raise ValueError(str(exc)) from exc
        for leg in resolved_intent.legs:
            if Decimal(str(leg.quantity)) <= 0:
                raise ValueError(
                    f"ZERO_QUANTITY: {leg.symbol} sized to {leg.quantity} after "
                    "instrument size_increment; refusing broker submission."
                )
        await self._audit_instruments_resolved(resolved_intent, signal_pk=signal_pk)
        return resolved_intent

    def _tws_client(self):
        oms = self._oms
        adapter = getattr(oms, "adapter", None) if oms is not None else None
        return getattr(adapter, "_client", None)

    async def _instrument_snapshot_for_legs(
        self, leg_specs: list[tuple[str | None, str | None]]
    ):
        from app.db.repositories.instrument_repository import (
            DatabaseInstrumentCatalog,
            SnapshotInstrumentCatalog,
        )
        from app.instruments.execution_override import execution_instrument_type
        from app.instruments.models import InstrumentResolutionError
        from app.instruments.resolver import ibkr_sec_type

        catalog = getattr(self, "_instrument_catalog", None)
        if catalog is None:
            return None
        rows = []
        for symbol, raw_type in leg_specs:
            if not symbol:
                continue
            try:
                execution_type, _override = execution_instrument_type(raw_type)
                sec = ibkr_sec_type(execution_type)
            except InstrumentResolutionError:
                continue
            if isinstance(catalog, DatabaseInstrumentCatalog):
                rows.extend(await catalog.find_all_async(symbol, sec))
            else:
                rows.extend(list(catalog.find_all(symbol, sec)))
        return SnapshotInstrumentCatalog(rows) if rows else SnapshotInstrumentCatalog([])

    async def _audit_instruments_resolved(
        self, intent: OrderIntent, *, signal_pk: int | None
    ) -> None:
        if self._session_factory is None:
            return
        from app.instruments.execution_override import execution_instrument_type

        legs = []
        for index, leg in enumerate(intent.legs):
            resolved = getattr(leg, "resolved", None)
            requested = getattr(
                resolved, "requested_instrument_type", None
            ) or leg.instrument_type
            resolved_type = getattr(resolved, "sec_type", None)
            _exec, override = execution_instrument_type(requested)
            logger.info(
                "Model Blue instrument resolution: symbol=%s requested_type=%s "
                "resolved_type=%s override=%s con_id=%s exchange=%s qty=%s",
                leg.symbol,
                requested,
                resolved_type,
                override or "none",
                getattr(resolved, "con_id", None),
                getattr(resolved, "exchange", None),
                leg.quantity,
            )
            legs.append(
                {
                    "index": index,
                    "symbol": leg.symbol,
                    "requested_instrument_type": requested,
                    "resolved_instrument_type": resolved_type,
                    "sec_type": resolved_type,
                    "override": override,
                    "con_id": getattr(resolved, "con_id", None),
                    "exchange": getattr(resolved, "exchange", None),
                    "quantity": str(leg.quantity),
                }
            )
        try:
            async with self._session_factory() as session, session.begin():
                await EventRepository(session).append(
                    process="instruments",
                    kind="INSTRUMENT_RESOLVED",
                    detail={
                        "trade_id": intent.signal_id,
                        "account_id": intent.account_id,
                        "legs": legs,
                    },
                    signal_id=signal_pk,
                    idempotency_key=f"instrument_resolved:{intent.account_id}:{intent.signal_id}",
                )
        except Exception:
            logger.exception("Failed to persist INSTRUMENT_RESOLVED event")
