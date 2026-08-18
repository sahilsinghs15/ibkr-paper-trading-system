"""Verified IBKR paper CFD master rows for the current demo symbols.

These conIds were discovered against paper Gateway as CFD, not STK
(SIL STK 211651690 / GDX STK 229726316).
"""

from decimal import Decimal

from app.instruments.models import InstrumentRecord

PAPER_CFD_INSTRUMENTS: tuple[InstrumentRecord, ...] = (
    InstrumentRecord(
        symbol="SIL",
        sec_type="CFD",
        trade_conid=384919303,
        market_data_conid=384919303,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal(1),
        underlying_exchange="ARCA",
        size_increment=Decimal("1.00000000"),
    ),
    InstrumentRecord(
        symbol="GDX",
        sec_type="CFD",
        trade_conid=134771127,
        market_data_conid=134771127,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal(1),
        underlying_exchange="ARCA",
        size_increment=Decimal("1.00000000"),
    ),
)
