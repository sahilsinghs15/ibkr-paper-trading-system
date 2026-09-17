"""Cancel Exposure validation.

When ``cancel_exposure`` is OFF (the safe default), an incoming pair signal must NOT
partially cancel an existing paired exposure.

Example:
  existing: AAPL BUY + EWC SELL  (one positions row, 2 legs)
  incoming: AAPL SELL + XYZ BUY   -> AAPL leg closes, XYZ does not close EWC -> REJECT
  incoming: AAPL SELL + EWC BUY   -> both legs close the same pair -> ALLOW
  incoming: AAPL BUY + XYZ SELL   -> AAPL same direction, not closing -> ALLOW

Only OPEN intents with 2 legs are subject to the check. CLOSE intents (legs==())
are explicit closes by trade_id and must not be rejected here.

The check is deliberately narrow: we only reject when at least one incoming leg
is the opposite side of an existing position's leg AND the other incoming leg
does not close that same position's corresponding leg.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identifiers import normalize_symbol
from app.db.models.position import PositionModel
from app.rms.models import OrderAction, OrderIntent, OrderSide

logger = logging.getLogger(__name__)


def _side_from_signed(signed_qty: Decimal | None) -> OrderSide | None:
    if signed_qty is None:
        return None
    try:
        val = Decimal(str(signed_qty))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return OrderSide.BUY if val > 0 else OrderSide.SELL if val < 0 else None


def _is_closing(incoming_side: OrderSide, existing_side: OrderSide | None) -> bool:
    if existing_side is None:
        return False
    return incoming_side != existing_side


def _position_legs(row: PositionModel) -> list[tuple[str, OrderSide]]:
    legs: list[tuple[str, OrderSide]] = []
    if row.leg_a_symbol and row.leg_a_signed_qty is not None:
        side = _side_from_signed(row.leg_a_signed_qty)
        if side is not None:
            legs.append((normalize_symbol(row.leg_a_symbol), side))
    if row.leg_b_symbol and row.leg_b_signed_qty is not None:
        side = _side_from_signed(row.leg_b_signed_qty)
        if side is not None:
            legs.append((normalize_symbol(row.leg_b_symbol), side))
    return legs


def evaluate_cancel_exposure(
    *,
    intent: OrderIntent,
    open_positions: list[PositionModel],
    cancel_exposure: bool,
) -> tuple[bool, str | None]:
    """Pure evaluation without DB access.

    Returns (allowed, reason). When ``cancel_exposure`` is True the check is
    disabled and always allowed.
    """
    if cancel_exposure:
        return True, None
    if intent.action != OrderAction.OPEN:
        return True, None
    if len(intent.legs) != 2:
        return True, None
    if not open_positions:
        return True, None

    # Normalize incoming legs: symbol -> side
    incoming = [
        (normalize_symbol(leg.symbol), leg.side) for leg in intent.legs if leg.symbol
    ]
    if len(incoming) != 2:
        return True, None

    # For each existing paired position, check closing count.
    # If any position is fully closed (2/2), the signal is an exact paired close -> ALLOW.
    fully_closed_positions: list[PositionModel] = []
    partially_cancelled: list[tuple[PositionModel, int]] = []

    for row in open_positions:
        pos_legs = _position_legs(row)
        if len(pos_legs) < 2:
            # Single-leg positions are not considered paired exposure for this rule.
            continue
        pos_map: dict[str, OrderSide] = {sym: side for sym, side in pos_legs}
        closing_count = 0
        for in_sym, in_side in incoming:
            existing_side = pos_map.get(in_sym)
            if existing_side is not None and _is_closing(in_side, existing_side):
                closing_count += 1
        if closing_count == 2:
            fully_closed_positions.append(row)
        elif closing_count == 1:
            partially_cancelled.append((row, closing_count))

    if fully_closed_positions:
        # Exact paired close exists -> must not reject.
        return True, None

    if partially_cancelled:
        row, _ = partially_cancelled[0]
        pos_legs = _position_legs(row)
        pos_desc = " + ".join(f"{sym} {side.value}" for sym, side in pos_legs)
        in_desc = " + ".join(f"{sym} {side.value}" for sym, side in incoming)
        reason = (
            "CANCEL_EXPOSURE_VIOLATION: cancel_exposure is OFF and incoming "
            f"pair [{in_desc}] partially cancels existing paired exposure "
            f"[{pos_desc}] (trade_id={row.trade_id} account_id={row.account_id} "
            f"strategy_id={row.strategy_id}). Only one side is being closed while "
            "the corresponding leg remains exposed. Set cancel_exposure=ON to permit."
        )
        return False, reason

    return True, None


async def check_cancel_exposure(
    *,
    intent: OrderIntent,
    account_id: int,
    cancel_exposure: bool,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    """DB-aware check. Raises ValueError on violation, otherwise returns normally.

    Safe to call from OrderManager fanout; performs a short read-only transaction.
    No broker calls, no row locks, minimal transaction scope.
    """
    if cancel_exposure:
        return
    if intent.action != OrderAction.OPEN:
        return
    if len(intent.legs) != 2:
        return
    if session_factory is None:
        return

    async with session_factory() as session:
        result = await session.execute(
            select(PositionModel).where(
                PositionModel.account_id == account_id,
                PositionModel.risk_state == "OPEN",
            )
        )
        open_positions = list(result.scalars().all())

    allowed, reason = evaluate_cancel_exposure(
        intent=intent, open_positions=open_positions, cancel_exposure=cancel_exposure
    )
    if not allowed and reason is not None:
        logger.info("Cancel exposure rejected: %s", reason)
        raise ValueError(reason)
