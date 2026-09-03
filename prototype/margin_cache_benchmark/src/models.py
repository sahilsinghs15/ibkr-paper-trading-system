"""Data models for benchmark — no DB dependency."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal


@dataclass(frozen=True)
class Instrument:
    instrument_type: str  # ETF or CFD
    symbol: str
    exchange: str
    currency: str

    def normalized_symbol(self) -> str:
        return self.symbol.strip().upper()

    def key(self) -> str:
        return f"{self.instrument_type}:{self.symbol}:{self.exchange}:{self.currency}"


@dataclass
class MarginResult:
    instrument_type: str
    symbol: str
    exchange: str
    currency: str
    con_id: int | None = None
    initial_margin: str = ""
    maintenance_margin: str = ""
    timestamp_utc: str = ""
    status: Literal["ok", "failed"] = "failed"
    error: str = ""
    # Timing diagnostics (not in CSV output header but in result JSON)
    contract_resolve_ms: float = 0.0
    margin_ms: float = 0.0
    total_ms: float = 0.0
    cached_contract: bool = False

    def to_csv_row(self) -> dict[str, str]:
        return {
            "instrument_type": self.instrument_type,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "currency": self.currency,
            "con_id": str(self.con_id) if self.con_id else "",
            "initial_margin": self.initial_margin,
            "maintenance_margin": self.maintenance_margin,
            "timestamp_utc": self.timestamp_utc,
            "status": self.status,
            "error": self.error,
        }


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BenchmarkStats:
    approach: str
    instruments: int
    workers: int
    rate_limit: float
    total_time_sec: float
    successes: int
    failures: int
    avg_latency_ms: float
    median_latency_ms: float
    p95_latency_ms: float
    total_requests: int
    pacing_errors: int
    retries: int
    contract_resolve_avg_ms: float = 0.0
    margin_avg_ms: float = 0.0
    actual_rate_per_sec: float = 0.0
    label: str = "MOCK RESULT"  # or REAL IBKR PAPER GATEWAY RESULT

    def to_dict(self) -> dict:
        return {
            "approach": self.approach,
            "instruments": self.instruments,
            "workers": self.workers,
            "rate_limit": self.rate_limit,
            "total_time_sec": round(self.total_time_sec, 3),
            "successes": self.successes,
            "failures": self.failures,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "median_latency_ms": round(self.median_latency_ms, 2),
            "p95_latency_ms": round(self.p95_latency_ms, 2),
            "total_requests": self.total_requests,
            "pacing_errors": self.pacing_errors,
            "retries": self.retries,
            "contract_resolve_avg_ms": round(self.contract_resolve_avg_ms, 2),
            "margin_avg_ms": round(self.margin_avg_ms, 2),
            "actual_rate_per_sec": round(self.actual_rate_per_sec, 2),
            "label": self.label,
        }
