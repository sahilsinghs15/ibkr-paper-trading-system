"""Account execution identity. No strategy math and no IBKR connection logic."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AccountExecutionContext:
    """One account's subscription to one strategy for a single signal.

    Routing produces a list of these. Sizing, RMS, and OMS consume one each.
    """

    account_id: int
    ibkr_account: str
    strategy_id: str
    total_margin: Decimal
    alloc_pct: Decimal
    committed_notional: Decimal
    target: Decimal
    stop: Decimal
    time_limit: int
    max_open_positions: int
