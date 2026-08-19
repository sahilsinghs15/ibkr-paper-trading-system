"""Model Blue strategy handler: parse, size, CLOSE reconstruction, persistence.

Generic RMS/OMS/IBKR never see Model Blue pair semantics — only OrderIntent.legs.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from inspect import isawaitable, iscoroutinefunction

from app.accounts.context import AccountExecutionContext
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.models.signal import Signal
from app.oms.models import ExecutionResult
from app.rms.models import OrderAction, OrderIntent, OrderLeg
from app.rms.models import OrderSide as RMSOrderSide
from app.services.model_blue.allocation import (
    CommittedCapitalProvider,
    TemporarySettingsCommittedCapitalProvider,
)
from app.services.model_blue.parser import (
    MODEL_BLUE_STRATEGY_ID,
    ModelBlueValidationError,
    is_model_blue_strategy,
    parse_model_blue_payload,
)
from app.services.model_blue.persistence import ModelBlueExecutionPersistence
from app.services.model_blue.sizer import ModelBlueSizer, SizedModelBlueLeg
from app.services.model_blue.trade_book import (
    InMemoryModelBlueTradeBook,
    ModelBlueTradeBook,
)
from app.services.strategies.handler import StrategyHandler

logger = logging.getLogger(__name__)

_STK_CONTRACT_MONTH = "2026-09"


class ModelBlueStrategy(StrategyHandler):
    """First registered strategy. Currently produces exactly two legs."""

    def __init__(
        self,
        *,
        committed_capital_provider: CommittedCapitalProvider | None = None,
        sizer: ModelBlueSizer | None = None,
        trade_book: ModelBlueTradeBook | None = None,
        persistence: ModelBlueExecutionPersistence | None = None,
    ) -> None:
        self._committed_capital_provider = committed_capital_provider
        self._sizer = sizer
        get_committed = getattr(committed_capital_provider, "get_committed", None)
        if (
            self._sizer is None
            and committed_capital_provider is not None
            and get_committed is not None
            and not iscoroutinefunction(get_committed)
        ):
            self._sizer = ModelBlueSizer(committed_capital_provider)
        self._trades: ModelBlueTradeBook = trade_book or InMemoryModelBlueTradeBook()
        self._persistence = persistence

    def can_handle(self, strategy_id: str | None) -> bool:
        return is_model_blue_strategy(strategy_id)

    def parse_payload(self, payload, *, timestamp, reason, raw_payload=None) -> Signal:
        return parse_model_blue_payload(
            payload,
            timestamp=timestamp,
            reason=reason,
            raw_payload=raw_payload,
        )

    async def build_intent(
        self,
        signal: Signal,
        account: AccountExecutionContext | None = None,
    ) -> OrderIntent:
        action_val = str(signal.action or "").upper()
        if action_val == "CLOSE":
            return await self._build_close_intent(signal, account)
        if action_val == "OPEN":
            return await self._build_open_intent(signal, account)
        raise ModelBlueValidationError(
            f"MODEL_BLUE_INVALID_ACTION: action must be OPEN or CLOSE, got '{signal.action}'."
        )

    async def after_submit(
        self,
        signal: Signal,
        intent: OrderIntent,
        exec_res: ExecutionResult,
    ) -> None:
        trade_id = (signal.trade_id or signal.signal_id or "").strip()
        if intent.action == OrderAction.OPEN:
            open_legs = tuple(
                OpenModelBlueTradeLeg(
                    symbol=leg.symbol,
                    instrument_type=leg.instrument_type,
                    side=leg.side,
                    quantity=Decimal(str(leg.quantity)),
                    price=leg.price,
                )
                for leg in intent.legs
            )
            trade = OpenModelBlueTrade(
                trade_id=trade_id,
                strategy_id=intent.strategy_id,
                direction=signal.direction or 0,
                legs=open_legs,
            )
            if self._persistence is not None:
                await self._persistence.persist_open(
                    signal,
                    trade,
                    exec_res.orders,
                    account_id=intent.account_id,
                )
            else:
                await self._trades.record_open(trade, account_id=intent.account_id)
        elif intent.action == OrderAction.CLOSE:
            if self._persistence is not None:
                await self._persistence.persist_close(
                    signal, trade_id, exec_res.orders, account_id=intent.account_id
                )
            else:
                await self._trades.close(trade_id, account_id=intent.account_id)

    async def _resolve_committed(self, strategy_id: str) -> Decimal | None:
        provider = self._committed_capital_provider
        if provider is None:
            return None
        result = provider.get_committed(strategy_id)
        if isawaitable(result):
            return await result
        return result

    async def _build_open_intent(
        self, signal: Signal, account: AccountExecutionContext | None
    ) -> OrderIntent:
        trade_id = (signal.trade_id or signal.signal_id or "").strip()
        if not trade_id:
            raise ModelBlueValidationError("MODEL_BLUE_MISSING_TRADE_ID: trade_id is required.")
        account_id = account.account_id if account is not None else None
        if await self._trades.get(trade_id, account_id=account_id) is not None:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_DUPLICATE_OPEN: trade_id '{trade_id}' is already open."
            )

        sizer = self._sizer
        if account is not None:
            sizer = ModelBlueSizer(
                TemporarySettingsCommittedCapitalProvider(account.committed_notional)
            )
        elif sizer is None:
            committed = await self._resolve_committed(
                signal.strategy_id or MODEL_BLUE_STRATEGY_ID
            )
            if committed is None or committed <= 0:
                raise ModelBlueValidationError(
                    "MODEL_BLUE_COMMITTED_NOT_CONFIGURED: no PostgreSQL allocation "
                    "(and no injected committed-capital provider) for Model Blue sizing."
                )
            sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(committed))

        sized = sizer.size_open(signal)
        legs = [self._order_leg_from_sized(index, leg) for index, leg in enumerate(sized)]
        if len(legs) != 2:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_INVALID_LEG_COUNT: OPEN requires exactly 2 legs, got {len(legs)}."
            )

        intent = OrderIntent(
            signal_id=trade_id,
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            action=OrderAction.OPEN,
            legs=legs,
            account_id=account.account_id if account is not None else None,
            ibkr_account=account.ibkr_account if account is not None else None,
            market=signal.market,
            timestamp=signal.timestamp or datetime.now(UTC),
        )
        logger.info(
            "Model Blue OPEN intent: trade_id=%s account_id=%s ibkr_account=%s legs=%s",
            trade_id,
            intent.account_id,
            intent.ibkr_account,
            [
                (leg.symbol, leg.side.value, leg.quantity, str(leg.notional))
                for leg in intent.legs
            ],
        )
        return intent

    async def _build_close_intent(
        self, signal: Signal, account: AccountExecutionContext | None
    ) -> OrderIntent:
        trade_id = (signal.trade_id or signal.signal_id or "").strip()
        if not trade_id:
            raise ModelBlueValidationError("MODEL_BLUE_MISSING_TRADE_ID: trade_id is required.")

        account_id = account.account_id if account is not None else None
        open_trade = await self._trades.get(trade_id, account_id=account_id)
        if open_trade is None:
            raise ModelBlueValidationError(
                f"NO_OPEN_POSITION: Cannot close Model Blue trade_id '{trade_id}'"
                + (f" for account_id={account_id}" if account_id is not None else "")
                + ": no matching open trade."
            )

        close_legs: list[OrderLeg] = []
        for index, open_leg in enumerate(open_trade.legs):
            close_side = (
                RMSOrderSide.SELL if open_leg.side == RMSOrderSide.BUY else RMSOrderSide.BUY
            )
            close_legs.append(
                OrderLeg(
                    symbol=open_leg.symbol,
                    side=close_side,
                    quantity=float(open_leg.quantity),
                    price=open_leg.price,
                    contract_month=_STK_CONTRACT_MONTH,
                    notional=open_leg.quantity * open_leg.price,
                    instrument_type=open_leg.instrument_type,
                    leg_index=index,
                )
            )

        logger.info(
            "Model Blue CLOSE intent: trade_id=%s account_id=%s legs=%s",
            trade_id,
            account_id,
            [
                (leg.symbol, leg.side.value, leg.quantity, str(leg.price))
                for leg in close_legs
            ],
        )
        return OrderIntent(
            signal_id=f"{trade_id}:CLOSE",
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            action=OrderAction.CLOSE,
            legs=close_legs,
            account_id=account.account_id if account is not None else None,
            ibkr_account=account.ibkr_account if account is not None else None,
            market=signal.market,
            timestamp=signal.timestamp or datetime.now(UTC),
        )

    def _order_leg_from_sized(self, index: int, sized: SizedModelBlueLeg) -> OrderLeg:
        return OrderLeg(
            symbol=sized.symbol,
            side=sized.side,
            quantity=float(sized.quantity),
            price=sized.price,
            contract_month=_STK_CONTRACT_MONTH,
            notional=sized.notional,
            instrument_type=sized.instrument_type,
            weight=sized.weight,
            leg_index=index,
        )
