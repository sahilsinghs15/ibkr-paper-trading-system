"""Resolved IBKR instrument identity. Requested type comes from the signal, not IBKR defaults."""

from dataclasses import dataclass
from decimal import Decimal


class InstrumentResolutionError(ValueError):
    """Contract cannot be resolved from the requested instrument type + metadata."""


@dataclass(frozen=True)
class InstrumentRecord:
    """Row from the instruments master (optional lookup)."""

    symbol: str
    sec_type: str
    trade_conid: int
    market_data_conid: int | None
    exchange: str
    currency: str
    multiplier: Decimal
    underlying_exchange: str | None = None
    size_increment: Decimal | None = None


@dataclass(frozen=True)
class ResolvedInstrument:
    """Executable IBKR contract fields derived from requested type + optional master row."""

    symbol: str
    requested_instrument_type: str
    sec_type: str
    exchange: str
    currency: str
    con_id: int | None = None
    market_data_con_id: int | None = None
    multiplier: Decimal = Decimal(1)
    primary_exchange: str | None = None
    size_increment: Decimal | None = None

    def identity_key(self) -> str:
        con = f":{self.con_id}" if self.con_id else ""
        return f"{self.symbol}-{self.sec_type}-{self.exchange}-{self.currency}{con}"
