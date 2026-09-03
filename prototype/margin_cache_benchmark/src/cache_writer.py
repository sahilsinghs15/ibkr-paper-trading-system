"""Cache CSV writer for margin results."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import MarginResult

CACHE_FIELDS = [
    "instrument_type",
    "symbol",
    "exchange",
    "currency",
    "con_id",
    "initial_margin",
    "maintenance_margin",
    "timestamp_utc",
    "status",
    "error",
]


def write_cache_csv(results: list[MarginResult], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CACHE_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow(r.to_csv_row())
    return path
