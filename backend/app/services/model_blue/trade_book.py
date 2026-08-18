"""In-memory and protocol for Model Blue open-trade lookup."""

from typing import Protocol

from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg

__all__ = [
    "InMemoryModelBlueTradeBook",
    "ModelBlueTradeBook",
    "OpenModelBlueTrade",
    "OpenModelBlueTradeLeg",
]


class ModelBlueTradeBook(Protocol):
    """Lookup/store open Model Blue trades. Implementations may be memory or DB."""

    async def get(self, trade_id: str) -> OpenModelBlueTrade | None: ...

    async def record_open(self, trade: OpenModelBlueTrade) -> None: ...

    async def close(self, trade_id: str) -> OpenModelBlueTrade: ...


class InMemoryModelBlueTradeBook:
    """Process-local trade_id → open pair map. Not durable across restarts.

    Retained for tests that do not wire PostgreSQL.
    """

    def __init__(self) -> None:
        self._trades: dict[str, OpenModelBlueTrade] = {}

    async def get(self, trade_id: str) -> OpenModelBlueTrade | None:
        return self._trades.get(trade_id)

    async def record_open(self, trade: OpenModelBlueTrade) -> None:
        self._trades[trade.trade_id] = trade

    async def close(self, trade_id: str) -> OpenModelBlueTrade:
        trade = self._trades.pop(trade_id, None)
        if trade is None:
            raise KeyError(trade_id)
        return trade
