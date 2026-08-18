"""Mock IBKR fills for OrderManager/basket tests. Never used against a real Gateway."""

import math
from typing import Any


def _fill_px(order: Any) -> float:
    raw = float(getattr(order, "lmtPrice", 0) or 0)
    if math.isfinite(raw) and 0 < raw < 1e12:
        return raw
    return 10.0


def fill_on_place_order(adapter: Any, client: Any) -> None:
    """Complete each placeOrder as a full fill via the adapter callback path."""

    def fake_place_order(order_id: int, contract: Any, order: Any) -> None:
        qty = float(getattr(order, "totalQuantity", 0) or 0)
        oms = adapter._orders_by_tws_id.get(order_id)
        if oms is not None and oms.limit_price is not None:
            px = float(oms.limit_price)
        else:
            px = _fill_px(order)
        adapter.on_order_status(
            order_id, "Filled", qty, 0.0, px, 0, 0, px, 1, "", 0.0
        )

    client.placeOrder.side_effect = fake_place_order
