"""OrderManager — application facade orchestrating Signal -> OrderIntent -> RMS -> OMS."""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.context import AccountExecutionContext
from app.accounts.router import DatabaseStrategyAccountRouter, StrategyAccountRouter
from app.db.models.account import PerSymbolLimitModel
from app.db.repositories.position_repository import PositionRepository
from app.db.repositories.signal_repository import SignalRepository
from app.models.signal import Signal, SignalType
from app.oms.models import AccountExecutionOutcome, ExecutionResult, FanoutExecutionResult, OMSOrder
from app.oms.oms_service import OMSService
from app.rms.engine import RMSEngine
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    RMSContext,
    RMSOutcome,
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
            for limit in limits:
                self._rms_context.per_symbol_limits[(limit.account_id, limit.symbol)] = (
                    limit.money_limit
                )

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

        strat_id = signal.strategy_id or self._strategy_id
        handler = self.registry.get(strat_id)
        if handler is None:
            result = await self._process_legacy_single_name(signal)
            return FanoutExecutionResult.from_single(result)

        if self._account_router is None:
            intent = await handler.build_intent(signal)
            result = await self._evaluate_and_submit(intent, signal, handler=handler)
            return FanoutExecutionResult.from_single(result)

        contexts = await self._account_router.resolve(strat_id)
        if not contexts:
            raise ValueError(
                f"NO_ELIGIBLE_ACCOUNTS: strategy '{strat_id}' has no enabled "
                "account subscriptions."
            )
        return await self._fanout_accounts(signal, handler, contexts)

    async def _fanout_accounts(
        self,
        signal: Signal,
        handler: StrategyHandler,
        contexts: list[AccountExecutionContext],
    ) -> FanoutExecutionResult:
        outcomes: list[AccountExecutionOutcome] = []
        raise_if_single = len(contexts) == 1
        for ctx in contexts:
            self._ensure_strategy_config(
                ctx.strategy_id, max_open_positions=ctx.max_open_positions
            )
            try:
                intent = await handler.build_intent(signal, account=ctx)
                result = await self._evaluate_and_submit(intent, signal, handler=handler)
                outcomes.append(
                    AccountExecutionOutcome(
                        account_id=ctx.account_id,
                        ibkr_account=ctx.ibkr_account,
                        result=result,
                    )
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
                outcomes.append(
                    AccountExecutionOutcome(
                        account_id=ctx.account_id,
                        ibkr_account=ctx.ibkr_account,
                        result=None,
                        error=str(exc),
                    )
                )
        return FanoutExecutionResult(outcomes=outcomes)

    async def _process_legacy_single_name(self, signal: Signal) -> ExecutionResult:
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
                    leg_index=0,
                )
            ],
            timestamp=signal.timestamp or datetime.now(UTC),
        )
        return await self._evaluate_and_submit(intent, signal, handler=None)

    async def _evaluate_and_submit(
        self,
        intent: OrderIntent,
        signal: Signal,
        *,
        handler: StrategyHandler | None,
    ) -> ExecutionResult:
        self._ensure_strategy_config(intent.strategy_id)

        rms_result = self._rms_engine.evaluate(intent, self._rms_context)
        if rms_result.outcome != RMSOutcome.PASS:
            msg = f"RMS check {rms_result.check_number} rejected intent: {rms_result.reason}"
            logger.warning(msg)
            raise ValueError(msg)

        evaluated_intent = rms_result.intent

        if self._oms is None:
            raise RuntimeError("No OMSService configured on OrderManager.")

        use_leg_prices = handler is not None and handler.uses_per_leg_prices()
        exec_res = await self._oms.submit_intent(
            intent=evaluated_intent,
            rms_result=rms_result,
            limit_price=None if use_leg_prices else (signal.price if signal.price is not None else self._price),
            order_type=self._order_type,
        )
        if not exec_res.success:
            raise RuntimeError(f"OMS submission failed: {exec_res.error_message}")

        await self._update_runtime_state(evaluated_intent, exec_res, handler=handler, sized_from=signal)
        return exec_res

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
