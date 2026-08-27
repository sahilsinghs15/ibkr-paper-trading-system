"""IBKR position snapshot types and collector for reqPositions."""

import threading
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BrokerPositionLine:
    """One non-zero IBKR position line from reqPositions."""

    ibkr_account: str
    symbol: str
    sec_type: str
    con_id: int
    currency: str
    exchange: str
    quantity: float
    avg_cost: float


@dataclass
class PositionSnapshotCollector:
    """Collects position callbacks until positionEnd."""

    lines: list[BrokerPositionLine] = field(default_factory=list)
    _done: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self) -> None:
        with self._lock:
            self.lines.clear()
        self._done.clear()

    def on_position(self, account: str, contract: object, position: float, avgCost: float) -> None:
        qty = float(position or 0)
        if abs(qty) <= 1e-9:
            return
        line = BrokerPositionLine(
            ibkr_account=str(account),
            symbol=str(getattr(contract, "symbol", "") or ""),
            sec_type=str(getattr(contract, "secType", "") or ""),
            con_id=int(getattr(contract, "conId", 0) or 0),
            currency=str(getattr(contract, "currency", "") or "USD"),
            exchange=str(getattr(contract, "exchange", "") or ""),
            quantity=qty,
            avg_cost=float(avgCost or 0),
        )
        with self._lock:
            self.lines.append(line)

    def on_position_end(self) -> None:
        self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout=timeout)

    def snapshot(self) -> list[BrokerPositionLine]:
        with self._lock:
            return list(self.lines)
