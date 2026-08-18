"""Strategy handler contract. Intentionally small — not a plugin framework."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from app.accounts.context import AccountExecutionContext
from app.models.signal import Signal
from app.oms.models import ExecutionResult
from app.rms.models import OrderIntent


class StrategyHandler(ABC):
    """Parses a webhook strategy and builds a generic multi-leg OrderIntent."""

    @abstractmethod
    def can_handle(self, strategy_id: str | None) -> bool:
        """Return True if this handler owns the strategy identity."""

    @abstractmethod
    def parse_payload(
        self,
        payload: dict[str, Any],
        *,
        timestamp: datetime,
        reason: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> Signal:
        """Parse strategy-specific JSON into a domain Signal."""

    @abstractmethod
    async def build_intent(
        self,
        signal: Signal,
        account: AccountExecutionContext | None = None,
    ) -> OrderIntent:
        """Size or reconstruct a generic OrderIntent (N legs) for one account."""

    def uses_per_leg_prices(self) -> bool:
        """When True, OMS uses each OrderLeg.price rather than a single override."""
        return True

    async def after_submit(
        self,
        signal: Signal,
        intent: OrderIntent,
        exec_res: ExecutionResult,
    ) -> None:
        """Optional persistence / trade-book bookkeeping after OMS success."""
        return None
