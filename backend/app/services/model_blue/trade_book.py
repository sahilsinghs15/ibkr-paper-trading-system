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

    async def get(
        self, trade_id: str, *, account_id: int | None = None
    ) -> OpenModelBlueTrade | None: ...

    async def record_open(
        self, trade: OpenModelBlueTrade, *, account_id: int | None = None
    ) -> None: ...

    async def close(
        self, trade_id: str, *, account_id: int | None = None
    ) -> OpenModelBlueTrade: ...


class InMemoryModelBlueTradeBook:
    """Process-local (account_id, trade_id) → open pair map. Not durable across restarts.

    Retained for tests that do not wire PostgreSQL.
    """

    def __init__(self) -> None:
        self._trades: dict[tuple[int, str], OpenModelBlueTrade] = {}

    def _key(self, trade_id: str, account_id: int | None) -> tuple[int, str]:
        return (account_id if account_id is not None else 0, trade_id)

    async def get(
        self, trade_id: str, *, account_id: int | None = None
    ) -> OpenModelBlueTrade | None:
        return self._trades.get(self._key(trade_id, account_id))

    async def record_open(
        self, trade: OpenModelBlueTrade, *, account_id: int | None = None
    ) -> None:
        self._trades[self._key(trade.trade_id, account_id)] = trade

    async def close(
        self, trade_id: str, *, account_id: int | None = None
    ) -> OpenModelBlueTrade:
        trade = self._trades.pop(self._key(trade_id, account_id), None)
        if trade is None:
            raise KeyError(trade_id)
        return trade
