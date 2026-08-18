"""Order Management System (OMS) domain models and lifecycle representations."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from app.rms.models import OrderIntent, OrderSide, RMSResult


class OMSOrderStatus(Enum):
    """Lifecycle status of an internal order in the OMS."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


# Clean alias for system-wide order status
OrderStatus = OMSOrderStatus


@dataclass
class ExecutionTimestamps:
    """Execution boundary timestamps captured in UTC."""

    intent_created_at: datetime | None = None
    rms_started_at: datetime | None = None
    rms_completed_at: datetime | None = None
    oms_received_at: datetime | None = None
    ibkr_submit_started_at: datetime | None = None
    ibkr_submit_completed_at: datetime | None = None
    order_status_received_at: datetime | None = None
    execution_received_at: datetime | None = None

    @property
    def rms_latency_ms(self) -> float | None:
        """RMS evaluation latency in milliseconds."""
        if self.rms_started_at and self.rms_completed_at:
            return (self.rms_completed_at - self.rms_started_at).total_seconds() * 1000.0
        return None

    @property
    def oms_latency_ms(self) -> float | None:
        """OMS processing duration before broker submission in milliseconds."""
        if self.oms_received_at and self.ibkr_submit_started_at:
            return (self.ibkr_submit_started_at - self.oms_received_at).total_seconds() * 1000.0
        return None

    @property
    def ibkr_submit_latency_ms(self) -> float | None:
        """Broker submission duration in milliseconds."""
        if self.ibkr_submit_started_at and self.ibkr_submit_completed_at:
            return (self.ibkr_submit_completed_at - self.ibkr_submit_started_at).total_seconds() * 1000.0
        return None

    @property
    def submit_to_fill_ms(self) -> float | None:
        """Time elapsed from submission to execution/fill callback in milliseconds."""
        start_ts = self.ibkr_submit_completed_at or self.ibkr_submit_started_at
        if start_ts and self.execution_received_at:
            return (self.execution_received_at - start_ts).total_seconds() * 1000.0
        return None

    @property
    def total_intent_to_fill_ms(self) -> float | None:
        """Total latency from OrderIntent creation to fill callback in milliseconds."""
        if self.intent_created_at and self.execution_received_at:
            return (self.execution_received_at - self.intent_created_at).total_seconds() * 1000.0
        return None


@dataclass
class OMSOrder:
    """Internal order domain representation maintained by OMS."""

    internal_order_id: str
    intent: OrderIntent
    symbol: str
    side: OrderSide
    quantity: float
    ibkr_order_id: int | str | None = None
    status: OMSOrderStatus = OMSOrderStatus.PENDING
    filled_quantity: float = 0
    remaining_quantity: float = 0
    average_fill_price: Decimal | None = None
    last_fill_price: Decimal | None = None
    limit_price: Decimal | None = None
    order_type: str = "LIMIT"
    error_message: str | None = None
    parent_signal_id: str | None = None
    leg_index: int | None = None
    timestamps: ExecutionTimestamps = field(default_factory=ExecutionTimestamps)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Ensure remaining quantity is initialized if 0 and unfilled."""
        if self.remaining_quantity == 0 and self.filled_quantity == 0:
            self.remaining_quantity = self.quantity

    @property
    def order_id(self) -> str:
        """Convenience property for API compatibility."""
        return self.internal_order_id

    @property
    def timestamp(self) -> datetime:
        """Convenience property for API compatibility."""
        return self.created_at

    @property
    def price(self) -> Decimal | None:
        """Convenience property for API compatibility."""
        return self.limit_price


# Clean alias for system-wide order
Order = OMSOrder


@dataclass
class ExecutionResult:
    """Execution summary returned by OMS to callers or developer harnesses."""

    order: OMSOrder
    rms_result: RMSResult
    success: bool
    error_message: str | None = None
    orders: list[OMSOrder] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.orders:
            self.orders = [self.order]
