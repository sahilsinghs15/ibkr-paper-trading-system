"""TEMPORARY paper/demo execution mapping.

TradingView / Model Blue continue to send instrument_type=STK.
When enabled, execution uses IBKR CFD from symbol + secType=CFD
(no instruments-table row, no invented conId).

Disable with PAPER_EXECUTE_STK_AS_CFD=false. Do not copy this mapping
into the IBKR adapter, TWS client, OMS placeOrder, basket, or RMS.
"""

from __future__ import annotations

STK_TO_CFD_DEMO = "STK_TO_CFD_DEMO"


def paper_execute_stk_as_cfd_enabled() -> bool:
    from app.core.config import get_settings

    return bool(get_settings().paper_execute_stk_as_cfd)


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
    on = paper_execute_stk_as_cfd_enabled() if enabled is None else enabled
    if on and raw == "STK":
        return "CFD", STK_TO_CFD_DEMO
    return raw, None
