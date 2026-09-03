"""Abstract IBKR client protocol."""

from __future__ import annotations

from typing import Protocol

from ..models import Instrument


class ContractDetailsResult(Protocol):
    con_id: int


class MarginResultData(Protocol):
    con_id: int
    initial_margin: str
    maintenance_margin: str


class IBKRClientBase(Protocol):
    """Protocol for IBKR clients (mock + real)."""

    async def resolve_contract(self, instrument: Instrument) -> tuple[int | None, float]:
        """Resolve contract via reqContractDetails. Returns (con_id, elapsed_ms)."""
        ...

    async def fetch_margin(self, instrument: Instrument, con_id: int | None) -> tuple[str, str, float]:
        """Fetch margin via whatIf order. Returns (initial, maintenance, elapsed_ms).
        MUST use whatIf — never transmit executable order.
        """
        ...

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
