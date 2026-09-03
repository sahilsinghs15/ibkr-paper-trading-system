"""Margin arithmetic for RMS check 1.

Required margin is abs(notional) * directional rate. Long and short legs are
summed, never netted: IBKR may net them, but per-leg rates cannot see the
offset, so overstating is the safe direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from app.rms.models import MarginPolicy, OrderIntent, OrderLeg, OrderSide

if TYPE_CHECKING:
    from app.services.account_margin import AccountMarginSnapshot

SOURCE_WHAT_IF = "WHAT_IF"
SOURCE_DEFAULT = "DEFAULT"

# Broker snapshot may or may not include a fill that landed at as_of.
# Holding a commitment slightly too long double-counts (conservative).
COMMITMENT_GRACE = timedelta(seconds=5)


class MarginBand(Enum):
    """Three-tier headroom classification for check 1 / Gate C."""

    COMFORTABLE = "COMFORTABLE"
    BORDERLINE = "BORDERLINE"
    INSUFFICIENT = "INSUFFICIENT"


def rate_for(
    symbol: str,
    instrument_type: str | None,
    side: OrderSide | str,
    rates: dict[tuple[str, str, str], Decimal],
    sources: dict[tuple[str, str, str], str],
    policy: MarginPolicy,
) -> tuple[Decimal, str]:
    """Return (rate, source) for one leg. Unknown symbols use default_rate."""
    side_key = side.value if isinstance(side, OrderSide) else str(side).upper()
    itype = (instrument_type or "STK").strip().upper() or "STK"
    key = (symbol.strip().upper(), itype, side_key)
    stored = rates.get(key)
    if stored is not None and stored > 0:
        source = sources.get(key, SOURCE_WHAT_IF)
        if source == SOURCE_WHAT_IF:
            return stored * policy.rate_safety_multiplier, SOURCE_WHAT_IF
        return stored, source
    return policy.default_rate, SOURCE_DEFAULT


def leg_required_margin(
    leg: OrderLeg,
    rates: dict[tuple[str, str, str], Decimal],
    sources: dict[tuple[str, str, str], str],
    policy: MarginPolicy,
) -> tuple[Decimal, str]:
    """abs(leg.effective_notional) * directional rate. Never negative."""
    rate, source = rate_for(
        leg.symbol, leg.instrument_type, leg.side, rates, sources, policy
    )
    return abs(leg.effective_notional) * rate, source


def estimate_required_margin(
    intent: OrderIntent,
    rates: dict[tuple[str, str, str], Decimal],
    sources: dict[tuple[str, str, str], str],
    policy: MarginPolicy,
) -> tuple[Decimal, dict[str, str]]:
    """Sum required margin across legs. Returns (total, {symbol: source})."""
    total = Decimal(0)
    per_leg: dict[str, str] = {}
    for leg in intent.legs:
        required, source = leg_required_margin(leg, rates, sources, policy)
        total += required
        per_leg[leg.symbol] = source
    return total, per_leg


def classify_headroom(
    required: Decimal,
    *,
    effective_free: Decimal,
    policy: MarginPolicy,
) -> MarginBand:
    """Map required vs usable headroom onto the three-tier band.

    usable = effective_free - min_free_buffer.
    required >= usable -> INSUFFICIENT
    required < usable * comfort_ratio -> COMFORTABLE
    else BORDERLINE
    """
    usable = effective_free - policy.min_free_buffer
    if required >= usable:
        return MarginBand.INSUFFICIENT
    if required < usable * policy.comfort_ratio:
        return MarginBand.COMFORTABLE
    return MarginBand.BORDERLINE


def pending_commitments(
    commitments: list[tuple[datetime, Decimal]],
    snapshot_as_of: datetime | None,
    *,
    grace: timedelta = COMMITMENT_GRACE,
) -> Decimal:
    """Sum commitments newer than snapshot.as_of - grace. Boundary is kept."""
    if not commitments:
        return Decimal(0)
    if snapshot_as_of is None:
        return sum((amount for _ts, amount in commitments), Decimal(0))
    cutoff = snapshot_as_of - grace
    total = Decimal(0)
    for ts, amount in commitments:
        committed_at = ts
        if committed_at.tzinfo is None:
            committed_at = committed_at.replace(tzinfo=UTC)
        if committed_at >= cutoff:
            total += amount
    return total


def headroom_floor(snapshot: AccountMarginSnapshot, policy: MarginPolicy) -> Decimal:
    """max(absolute buffer, pct of net liquidation)."""
    floor = policy.min_free_buffer
    if snapshot.net_liquidation is not None and policy.min_free_pct_of_netliq > 0:
        floor = max(floor, snapshot.net_liquidation * policy.min_free_pct_of_netliq)
    return floor


def effective_free_margin(
    snapshot: AccountMarginSnapshot,
    commitments: list[tuple[datetime, Decimal]],
    policy: MarginPolicy,
) -> Decimal | None:
    free = snapshot.free_margin(policy.gate_basis)
    if free is None:
        return None
    pending = pending_commitments(commitments, snapshot.as_of)
    return free - pending


def look_ahead_effective_free(
    snapshot: AccountMarginSnapshot,
    commitments: list[tuple[datetime, Decimal]],
    policy: MarginPolicy,
) -> Decimal | None:
    free = snapshot.look_ahead_free_margin(policy.gate_basis)
    if free is None:
        return None
    pending = pending_commitments(commitments, snapshot.as_of)
    return free - pending


@dataclass(frozen=True)
class HeadroomView:
    """Snapshot + tally view used by Gate A, check 1, and Gate C."""

    snapshot_free: Decimal
    pending: Decimal
    effective_free: Decimal
    floor: Decimal
    look_ahead_free: Decimal | None
    look_ahead_effective: Decimal | None
