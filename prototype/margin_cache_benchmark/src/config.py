"""Prototype configuration — isolated from production Settings (app.core.config)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for margin-cache prototype.

    All values are isolated from production ``app.core.config.Settings``.
    Uses ``MARGIN_CACHE_`` prefix to avoid env collisions.
    """

    ib_host: str = "127.0.0.1"
    # Paper Gateway port — NEVER 4001 for this prototype
    ib_port: int = 4002
    ib_client_id: int = 99

    # Rate limit — requests per second (token bucket)
    cache_rate_limit: float = 10.0
    max_wait_sec: float = 8.0

    # Timeouts
    contract_details_timeout: float = 5.0
    margin_timeout: float = 8.0

    # Instrument CSV path
    csv_path: str = "data/instruments.csv"

    # Worker defaults
    default_workers: int = 2

    @classmethod
    def from_env(cls, csv_path: str | None = None) -> BenchmarkConfig:
        return cls(
            ib_host=os.environ.get("MARGIN_CACHE_IB_HOST", "127.0.0.1"),
            ib_port=int(os.environ.get("MARGIN_CACHE_IB_PORT", "4002")),
            ib_client_id=int(os.environ.get("MARGIN_CACHE_IB_CLIENT_ID", "99")),
            cache_rate_limit=float(os.environ.get("MARGIN_CACHE_RATE_LIMIT", "10.0")),
            max_wait_sec=float(os.environ.get("MARGIN_CACHE_MAX_WAIT_SEC", "8.0")),
            contract_details_timeout=float(os.environ.get("MARGIN_CACHE_CONTRACT_TIMEOUT", "5.0")),
            margin_timeout=float(os.environ.get("MARGIN_CACHE_MARGIN_TIMEOUT", "8.0")),
            csv_path=csv_path or os.environ.get("MARGIN_CACHE_CSV", "data/instruments.csv"),
            default_workers=int(os.environ.get("MARGIN_CACHE_WORKERS", "2")),
        )

    def validate(self) -> None:
        if self.ib_port == 4001:
            raise ValueError("MARGIN_CACHE_IB_PORT must not be 4001 (live). Use 4002 paper.")
        if self.cache_rate_limit <= 0:
            raise ValueError("cache_rate_limit must be > 0")
