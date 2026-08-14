"""Data domain models for the Risk Management System (RMS)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum


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
    """Represents a single leg of an order intent.

    Attributes:
        symbol: The instrument symbol (e.g., 'AAPL', 'RELIANCE').
        side: BUY or SELL.
        quantity: Order quantity (must be positive).
        price: Unit price.
        contract_month: Expiry/contract month formatted as 'YYYY-MM' (e.g., '2026-09').
        con_id: Optional IBKR contract identifier.
        notional: Optional explicit notional value; if None, computed as quantity * price.
    """

    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    contract_month: str
    con_id: int | None = None
    notional: Decimal | None = None

    @property
    def effective_notional(self) -> Decimal:
        """Calculate effective notional amount for the leg."""
        if self.notional is not None:
            return self.notional
        return Decimal(self.quantity) * self.price


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
    account_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


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
        processed_signals: Set of processed (strategy_id, signal_id) tuples for duplicate detection.
        strategy_configs: Dictionary mapping strategy_id to StrategyConfig.
        open_positions: Dictionary mapping strategy_id to current open position count.
        symbol_exposures: Dictionary mapping symbol to current monetary exposure.
        current_time: Evaluation reference timestamp.
        rollover_window_days: Days before contract month expiry during which rollover is active.
        target_rollover_month: Optional explicit next contract month string for rollover adjustments.
        rollover_checker: Optional custom callback to evaluate if a contract month is in rollover.
    """

    processed_signals: set[tuple[str, str]] = field(default_factory=set)
    strategy_configs: dict[str, StrategyConfig] = field(default_factory=dict)
    open_positions: dict[str, int] = field(default_factory=dict)
    symbol_exposures: dict[str, Decimal] = field(default_factory=dict)
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
