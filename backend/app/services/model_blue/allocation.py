"""Committed-capital providers for Model Blue sizing.

Production (DB-2): DatabaseCommittedCapitalProvider reads PostgreSQL
`accounts.total_margin * allocations.alloc_pct`. Missing row → reject.

Tests may still inject TemporarySettingsCommittedCapitalProvider.
That env/settings path is not used when the database provider is wired.
"""

from decimal import Decimal
from typing import Protocol

from app.services.model_blue.parser import is_model_blue_strategy


class CommittedCapitalProvider(Protocol):
    """Resolves the base-leg committed notional for a strategy.

    Future OEMS/DB implementations should satisfy this protocol.
    """

    def get_committed(self, strategy_id: str) -> Decimal | None:
        """Return committed USD notional for the base leg, or None if unset."""
        ...


class TemporarySettingsCommittedCapitalProvider:
    """TEMPORARY paper-phase committed notional from process configuration.

    This is not live-account allocation. A missing/non-positive value must
    cause Model Blue OPEN to be rejected — never invent a financial amount.
    """

    def __init__(self, committed_notional: Decimal | None) -> None:
        self._committed_notional = committed_notional

    def get_committed(self, strategy_id: str) -> Decimal | None:
        if not is_model_blue_strategy(strategy_id):
            return None
        if self._committed_notional is None or self._committed_notional <= 0:
            return None
        return self._committed_notional
