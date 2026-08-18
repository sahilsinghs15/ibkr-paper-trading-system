"""Data domain models for the Risk Management System (RMS)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.instruments.models import ResolvedInstrument


class OrderAction(Enum):
    """Action of an order intent."""

    OPEN = "OPEN"
    CLOSE = "CLOSE"


class OrderSide(Enum):
    """Side of an order leg."""

    BUY = "BUY"
    SELL = "SELL"


class RMSOutcome(Enum):
    """Outcome status of RMS evaluation."""

    PASS = "PASS"
    REJECT = "REJECT"
    ADJUST = "ADJUST"
    HALT = "HALT"


@dataclass(frozen=True)
class OrderLeg:
    """Generic execution leg. RMS, OMS, and IBKR iterate ``intent.legs`` for N >= 1.

    Strategy-specific fields (for example Model Blue weight) stay optional so
    they are not required of every future strategy.
    """

    symbol: str
    side: OrderSide
    quantity: float
    price: Decimal
    contract_month: str
    con_id: int | None = None
    notional: Decimal | None = None
    instrument_type: str | None = None
    weight: float | None = None
    leg_index: int | None = None
    metadata: dict[str, Any] | None = None
    exchange: str | None = None
    currency: str | None = None
    resolved: "ResolvedInstrument | None" = None

    @property
    def effective_notional(self) -> Decimal:
        """Calculate effective notional amount for the leg."""
        if self.notional is not None:
            return self.notional
        return Decimal(str(self.quantity)) * self.price


@dataclass(frozen=True)
class OrderIntent:
    """Sized trading intent passed to RMS prior to Order Management System submission.

    Attributes:
        signal_id: Unique identifier of the originating signal.
        strategy_id: Identifier of the strategy (e.g., 'MODEL_BLUE').
        action: Action type (OPEN or CLOSE).
        legs: List of order legs associated with the intent.
        account_id: Optional future account identifier.
        timestamp: Creation timestamp.
    """

    signal_id: str
    strategy_id: str
    action: OrderAction
    legs: list[OrderLeg]
    account_id: int | None = None
    ibkr_account: str | None = None
    market: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


def duplicate_lookup_key(intent: OrderIntent) -> tuple:
    """Account-scoped duplicate key when account_id is present."""
    if intent.account_id is not None:
        return (intent.account_id, intent.strategy_id, intent.signal_id)
    return (intent.strategy_id, intent.signal_id)


def open_position_key(intent: OrderIntent) -> str | tuple[int, str]:
    """Account-scoped open-count key when account_id is present."""
    if intent.account_id is not None:
        return (intent.account_id, intent.strategy_id)
    return intent.strategy_id


def exposure_key(intent: OrderIntent, symbol: str) -> str | tuple[int, str]:
    """Account-scoped symbol exposure key when account_id is present."""
    if intent.account_id is not None:
        return (intent.account_id, symbol)
    return symbol


# Generic aliases: execution pipeline operates on List[TradeLeg], not leg_a/leg_b.
TradeLeg = OrderLeg


@dataclass(frozen=True)
class StrategyConfig:
    """Risk configuration associated with a specific strategy.

    Attributes:
        strategy_id: Identifier of the strategy.
        max_open_positions: Maximum allowed open positions for the strategy.
        money_limit_per_symbol: Optional per-symbol money budget limit for this strategy/account.
    """

    strategy_id: str
    max_open_positions: int
    money_limit_per_symbol: Decimal | None = None


@dataclass
class RMSContext:
    """Simulated state and configuration context provided to RMS for evaluation.

    Attributes:
        processed_signals: Duplicate keys. Without account_id: (strategy_id, signal_id).
            With account: (account_id, strategy_id, signal_id).
        strategy_configs: Dictionary mapping strategy_id to StrategyConfig.
        open_positions: Counts keyed by strategy_id or (account_id, strategy_id).
        symbol_exposures: Exposure keyed by symbol or (account_id, symbol).
        per_symbol_limits: Account-specific (account_id, symbol) money limits.
        current_time: Evaluation reference timestamp.
        rollover_window_days: Days before contract month expiry during which rollover is active.
        target_rollover_month: Optional explicit next contract month string for rollover adjustments.
        rollover_checker: Optional custom callback to evaluate if a contract month is in rollover.
    """

    processed_signals: set[tuple] = field(default_factory=set)
    strategy_configs: dict[str, StrategyConfig] = field(default_factory=dict)
    open_positions: dict[str | tuple[int, str], int] = field(default_factory=dict)
    symbol_exposures: dict[str | tuple[int, str], Decimal] = field(default_factory=dict)
    per_symbol_limits: dict[tuple[int, str], Decimal] = field(default_factory=dict)
    account_open_limits: dict[tuple[int, str], int] = field(default_factory=dict)
    current_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    rollover_window_days: int = 7
    target_rollover_month: str | None = None
    rollover_checker: Callable[[str, datetime, int], bool] | None = None


@dataclass(frozen=True)
class CheckResult:
    """Result of an individual RMS check evaluation.

    Attributes:
        check_number: Check sequence number (e.g., 2, 3, 4, 7, 8).
        check_name: Human-readable name of the check.
        outcome: Outcome status (PASS, REJECT, ADJUST, HALT).
        reason: Description of rejection or adjustment rationale.
        adjusted_intent: New OrderIntent if outcome is ADJUST.
    """

    check_number: int
    check_name: str
    outcome: RMSOutcome
    reason: str | None = None
    adjusted_intent: OrderIntent | None = None


@dataclass(frozen=True)
class RMSResult:
    """Final outcome of RMS engine evaluation for an OrderIntent.

    Attributes:
        outcome: Final outcome (PASS, REJECT, ADJUST, HALT).
        intent: The final (possibly adjusted) OrderIntent.
        original_intent: The original un-adjusted OrderIntent.
        check_number: The check number that caused failure, if outcome is REJECT or HALT.
        reason: Failure or adjustment description.
        check_results: Complete audit trail of individual check evaluations.
        timestamp: Evaluation completion timestamp.
    """

    outcome: RMSOutcome
    intent: OrderIntent
    original_intent: OrderIntent
    check_number: int | None = None
    reason: str | None = None
    check_results: list[CheckResult] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
