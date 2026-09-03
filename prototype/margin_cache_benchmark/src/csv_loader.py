"""CSV loading / validation / duplicate detection — no DB."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import Instrument

VALID_TYPES = {"ETF", "CFD"}
VALID_ETF_EXCHANGES = {"ARCA", "AMEX", "NYSE ARCA"}  # NYSE ARCA normalized to ARCA


def _normalize_exchange(raw: str) -> str:
    v = raw.strip().upper()
    if v == "NYSE ARCA":
        return "ARCA"
    return v


def load_instruments(csv_path: str | Path) -> list[Instrument]:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Instrument CSV not found: {path}")
    instruments: list[Instrument] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"instrument_type", "symbol", "exchange", "currency"}
        if reader.fieldnames is None or not required.issubset({h.strip().lower() for h in reader.fieldnames}):
            raise ValueError(f"CSV missing required columns {required}, got {reader.fieldnames}")
        # normalize fieldnames
        lower_map = {h.strip().lower(): h for h in reader.fieldnames}
        for i, row in enumerate(reader, start=2):
            itype = row[lower_map["instrument_type"]].strip().upper()
            symbol = row[lower_map["symbol"]].strip().upper()
            exchange = _normalize_exchange(row[lower_map["exchange"]])
            currency = row[lower_map["currency"]].strip().upper()
            if not itype or not symbol or not exchange or not currency:
                raise ValueError(f"Row {i}: empty field — {row}")
            if itype not in VALID_TYPES:
                raise ValueError(f"Row {i}: invalid instrument_type {itype!r} — must be ETF or CFD")
            if itype == "ETF" and exchange not in ("ARCA", "AMEX"):
                raise ValueError(f"Row {i}: ETF exchange must be ARCA or AMEX, got {exchange!r}")
            # CFD exchange SMART is recommended but allow others for flexibility
            key = f"{itype}:{symbol}:{exchange}:{currency}"
            if key in seen:
                duplicates.append(key)
            else:
                seen.add(key)
            instruments.append(Instrument(itype, symbol, exchange, currency))
    if duplicates:
        raise ValueError(f"Duplicate instruments detected: {duplicates[:5]} (total {len(duplicates)})")
    return instruments


def write_instruments(instruments: list[Instrument], csv_path: str | Path) -> None:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["instrument_type", "symbol", "exchange", "currency"])
        writer.writeheader()
        for inst in instruments:
            writer.writerow(
                {
                    "instrument_type": inst.instrument_type,
                    "symbol": inst.symbol,
                    "exchange": inst.exchange,
                    "currency": inst.currency,
                }
            )
