"""Prune raw TradingView webhook capture files.

Every webhook writes a JSON capture under ``backend/data/tradingview_webhooks/``
(see ``app/api/routes/webhooks.py``). Nothing deletes them, so the directory grows
for the life of the host. Captures are a debugging aid only -- the durable record
of a signal is Postgres (``signals``, ``signal_jobs``), so pruning is safe.

Dry run is the default; pass --apply to actually delete.

    .venv/bin/python scripts/prune_webhook_captures.py --days 14
    .venv/bin/python scripts/prune_webhook_captures.py --days 14 --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("prune_webhook_captures")

# Mirrors WEBHOOK_CAPTURE_DIR in app/api/routes/webhooks.py.
CAPTURE_DIR = Path(__file__).resolve().parents[1] / "data" / "tradingview_webhooks"
DEFAULT_RETENTION_DAYS = 14


def prune(
    capture_dir: Path, retention_days: int, apply: bool
) -> tuple[int, int, int]:
    """Delete captures older than the retention window.

    Returns (scanned, matched, bytes_freed). With apply=False nothing is removed.
    """
    if not capture_dir.is_dir():
        logger.info("No capture directory at %s -- nothing to do.", capture_dir)
        return (0, 0, 0)

    cutoff = time.time() - (retention_days * 86400)
    scanned = 0
    matched = 0
    freed = 0

    for path in capture_dir.glob("webhook_*.json"):
        scanned += 1
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            continue
        matched += 1
        freed += stat.st_size
        if apply:
            try:
                path.unlink()
            except OSError:
                logger.warning("Could not delete %s", path.name)

    return (scanned, matched, freed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Retention window in days (default {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=CAPTURE_DIR,
        help="Capture directory to prune",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete (default is a dry run)",
    )
    args = parser.parse_args()

    if args.days < 0:
        parser.error("--days must be >= 0")

    scanned, matched, freed = prune(args.dir, args.days, args.apply)
    verb = "Deleted" if args.apply else "Would delete"
    logger.info(
        "%s %d of %d capture file(s) older than %d day(s), freeing %.1f MB from %s",
        verb,
        matched,
        scanned,
        args.days,
        freed / (1024 * 1024),
        args.dir,
    )
    if matched and not args.apply:
        logger.info("Dry run only. Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
