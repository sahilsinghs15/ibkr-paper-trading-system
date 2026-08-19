"""Discover IBKR CFD conIds for symbols and upsert instruments master."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_backend_dir = str(Path(__file__).resolve().parent.parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import get_settings
from app.core.logger import setup_logging
from app.db.repositories.instrument_repository import DatabaseInstrumentCatalog
from app.db.session import AsyncSessionLocal
from app.instruments.cfd_discover import ensure_cfd_instruments_for_symbols


async def _run(
    symbols: list[str],
    *,
    host: str,
    port: int,
    client_id: int,
    timeout: float,
) -> int:
    settings = get_settings()
    client = TWSClient()
    connected = client.connect_and_start(
        host=host,
        port=port,
        client_id=client_id,
        timeout=float(settings.ibkr_connection_timeout),
    )
    if not connected:
        print("FAILURE: could not connect to TWS/Gateway", file=sys.stderr)
        return 1
    catalog = DatabaseInstrumentCatalog(AsyncSessionLocal)
    try:
        rows = await ensure_cfd_instruments_for_symbols(
            symbols=symbols,
            client=client,
            session_factory=AsyncSessionLocal,
            catalog=catalog,
            timeout=timeout,
        )
    finally:
        client.disconnect_clean()
    if not rows:
        print("No new CFD instruments discovered (missing, ambiguous, or already present).")
        return 0
    for row in rows:
        print(
            f"{row.symbol} sec_type={row.sec_type} trade_conid={row.trade_conid} "
            f"market_data_conid={row.market_data_conid} exchange={row.exchange}"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover IBKR CFD conIds and upsert instruments master."
    )
    parser.add_argument(
        "symbols",
        nargs="+",
        help="Symbols to discover (e.g. XLE XOP SIL GDX)",
    )
    parser.add_argument("--host", default=None, help="TWS host (default from Settings)")
    parser.add_argument("--port", type=int, default=None, help="TWS port (default from Settings)")
    parser.add_argument(
        "--client-id",
        type=int,
        default=99,
        help="API client id (default 99, avoid clashing with trading app)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-symbol contract-details timeout seconds",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(level="DEBUG" if args.verbose else "INFO", filename_prefix="discover-cfd")
    settings = get_settings()
    host = args.host or settings.ibkr_host
    port = args.port or settings.ibkr_port
    code = asyncio.run(
        _run(
            [s.strip().upper() for s in args.symbols if s.strip()],
            host=host,
            port=port,
            client_id=args.client_id,
            timeout=args.timeout,
        )
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
