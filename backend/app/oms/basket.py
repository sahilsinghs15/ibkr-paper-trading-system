"""Basket-level atomicity for multi-leg execution. Independent of Model Blue."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from app.oms.models import OMSOrder
from app.rms.models import OrderIntent


class BasketState(str, Enum):
    """Basket lifecycle. Child OMS orders keep their own PENDING/SUBMITTED/FILLED machine."""

    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNWINDING = "UNWINDING"
    COMPENSATED = "COMPENSATED"
    CRITICAL = "CRITICAL"


@dataclass
class Basket:
    """One account's attempt to execute one trade_id as an N-leg basket."""

    account_id: int | None
    trade_id: str
    strategy_id: str
    action: str
    intended_leg_count: int
    state: BasketState = BasketState.PENDING
    id: int | None = None
    signal_pk: int | None = None
    orders: list[OMSOrder] = field(default_factory=list)
    compensation_orders: list[OMSOrder] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class BasketExecutionResult:
    """Outcome of BasketCoordinator.execute for one account intent."""

    basket: Basket
    intent: OrderIntent
    orders: list[OMSOrder]
    compensation_orders: list[OMSOrder] = field(default_factory=list)

    @property
    def state(self) -> BasketState:
        return self.basket.state

    @property
    def success(self) -> bool:
        return self.basket.state in (BasketState.OPEN, BasketState.CLOSED)
