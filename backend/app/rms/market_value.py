"""Market-value arithmetic for RMS check 101.

Market value is gross dollar exposure: sum of abs(quantity) * price across
legs. It is NOT margin -- margin is the collateral IBKR requires against this
exposure, computed by their margin model, and no code here estimates it.

Pure functions, no DB or broker access.
"""

from decimal import Decimal

from app.rms.models import OrderIntent, OrderLeg


def leg_market_value(leg: OrderLeg) -> Decimal:
    """Absolute market value of one leg. Never negative."""
    return abs(leg.effective_notional)


def intent_market_value(intent: OrderIntent) -> Decimal:
    """Total market value across every leg of an intent.

    Long and short legs are summed, not netted: a market-neutral pair still
    carries gross exposure on both sides.
    """
    return sum((leg_market_value(leg) for leg in intent.legs), Decimal(0))


def position_row_market_value(row) -> Decimal:
    """Market value of one open positions row, from entry marks.

    Entry marks, not live marks: this is capital deployed at entry, not a
    valuation. Both legs summed.
    """
    total = abs(Decimal(str(row.leg_a_signed_qty))) * abs(
        Decimal(str(row.leg_a_entry_mark))
    )
    if (
        row.leg_b_symbol
        and row.leg_b_signed_qty is not None
        and row.leg_b_entry_mark is not None
    ):
        total += abs(Decimal(str(row.leg_b_signed_qty))) * abs(
            Decimal(str(row.leg_b_entry_mark))
        )
    return total
