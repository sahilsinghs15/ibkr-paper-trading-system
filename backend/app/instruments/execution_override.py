"""Production Model Blue execution mapping.

TradingView / Model Blue continue to send instrument_type=STK.
When enabled, execution uses IBKR CFD from symbol + secType=CFD
(no instruments-table row, no invented conId).

This is the intended production instrument for Model Blue; options
are added later as a new requested type. Do not copy this mapping
into the IBKR adapter, TWS client, OMS placeOrder, basket, or RMS.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

STK_TO_CFD = "STK_TO_CFD"


def execute_stk_as_cfd_enabled() -> bool:
    from app.core.config import get_settings

    settings = get_settings()
    return bool(getattr(settings, "execute_stk_as_cfd", True))


def execution_instrument_type(
    requested: str | None,
    *,
    enabled: bool | None = None,
) -> tuple[str, str | None]:
    """Return (type used for IBKR execution, override name or None).

    Does not mutate the original requested type.
    """
    raw = (requested or "").strip().upper()
    if not raw:
        return raw, None
    on = execute_stk_as_cfd_enabled() if enabled is None else enabled
    if on and raw == "STK":
        logger.info(
            "Executed secType=CFD for requested STK (Model Blue STK→CFD map)"
        )
        return "CFD", STK_TO_CFD
    return raw, None
