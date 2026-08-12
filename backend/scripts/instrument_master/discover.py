"""This script is read-only and must never place, modify, or cancel orders."""

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ibapi.client import EClient  # type: ignore[import-untyped]
from ibapi.contract import Contract, ContractDetails  # type: ignore[import-untyped]
from ibapi.wrapper import EWrapper  # type: ignore[import-untyped]

# Ensure backend directory is in sys.path for relative package imports
_backend_dir = str(Path(__file__).resolve().parent.parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from scripts.instrument_master.pacer import RatePacer


@dataclass
class RequestMetric:
    """Detailed timing and status metrics for a single API contract request."""

    seed_symbol: str
    req_id: int
    sec_type: str
    is_fallback: bool
    category: str
    status: str
    error_code: str
    pacer_wait_ms: float
    tws_rtt_ms: float
    total_latency_ms: float
    num_contracts: int = 0


from scripts.instrument_master.seed_fetcher import (
    SeedRecord,
    fetch_nasdaq_seed_universe,
)

# Default seed universe for first POC
DEFAULT_SEED_SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "SPY"]

# CSV Field Header Names (Combined external seed metadata + execution status + canonical IBKR fields)
CSV_FIELDNAMES = [
    "seed_symbol",
    "seed_raw_symbol",
    "seed_security_name",
    "seed_category",
    "seed_exchange",
    "seed_is_etf",
    "seed_source_file",
    "status",
    "error_code",
    "error_message",
    "con_id",
    "symbol",
    "local_symbol",
    "sec_type",
    "exchange",
    "primary_exchange",
    "currency",
    "trading_class",
    "multiplier",
    "expiry",
    "strike",
    "right",
    "min_tick",
    "trading_hours",
    "liquid_hours",
    "time_zone_id",
    "underlying_con_id",
    "description",
    "retrieved_at",
]

logger = logging.getLogger("instrument_master_discover")


def setup_logging(verbose: bool = False) -> None:
    """Configure console logging."""
    log_level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.setLevel(log_level)
    logger.addHandler(handler)


class InstrumentDiscoveryClient(EWrapper, EClient):
    """Read-only IBKR client wrapper for discovering contract details."""

    def __init__(self) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        self._connected_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Maps reqId -> list of ContractDetails
        self.contract_details_results: dict[int, list[ContractDetails]] = {}
        # Tracks completion event for each reqId
        self._request_events: dict[int, threading.Event] = {}
        # Tracks errors for each reqId
        self.request_errors: dict[int, str] = {}
        # Connection status flag
        self.connection_error: str | None = None

    def nextValidId(self, orderId: int) -> None:
        """Callback received when API connection handshake completes."""
        super().nextValidId(orderId)
        self._connected_event.set()
        logger.info("Connection handshake successful. Next order ID: %d", orderId)

    def error(self, *args: Any, **kwargs: Any) -> None:
        """Callback received when TWS signals an error or status notification.

        Compatible with both 3-arg and 4-arg callback signatures across ibapi versions.
        `args` can be:
            (reqId, errorCode, errorString)
        or
            (reqId, errorCode, errorString, advancedOrderRejectJson)
        """
        try:
            if len(args) >= 3:
                super().error(args[0], args[1], args[2])
            elif args:
                super().error(*args)
            elif kwargs:
                base_kwargs = {
                    k: v for k, v in kwargs.items() if k != "advancedOrderRejectJson"
                }
                super().error(**base_kwargs)
        except TypeError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("Error delegating to super().error: %s", e)

        try:
            reqId: int = args[0] if len(args) > 0 else kwargs.get("reqId", -1)
            errorCode: int = args[1] if len(args) > 1 else kwargs.get("errorCode", -1)
            errorString: str = (
                args[2] if len(args) > 2 else kwargs.get("errorString", "")
            )
            advancedOrderRejectJson: str = (
                args[3] if len(args) > 3 else kwargs.get("advancedOrderRejectJson", "")
            )

            # Codes 2000-2999 are informational/status notifications (e.g. market data farm connection)
            if 2000 <= errorCode < 3000:
                logger.info(
                    "TWS Info Code %d (reqId=%d): %s",
                    errorCode,
                    reqId,
                    errorString,
                )
                return

            reject_info = (
                f" [AdvancedReject: {advancedOrderRejectJson}]"
                if advancedOrderRejectJson
                else ""
            )
            logger.warning(
                "TWS Error Code %d (reqId=%d): %s%s",
                errorCode,
                reqId,
                errorString,
                reject_info,
            )

            with self._lock:
                err_msg = f"Code {errorCode}: {errorString}"
                if advancedOrderRejectJson:
                    err_msg += f" (RejectJson: {advancedOrderRejectJson})"

                if reqId in self._request_events:
                    self.request_errors[reqId] = err_msg
                    # Unblock waiting thread on request error
                    self._request_events[reqId].set()
                elif reqId == -1:
                    # General socket / connection error
                    self.connection_error = err_msg
                else:
                    logger.debug(
                        "Stale error callback ignored for unregistered reqId=%d: %s",
                        reqId,
                        err_msg,
                    )
        except Exception:
            logger.exception("Unexpected exception in error callback handler")

    def connectionClosed(self) -> None:
        """Callback when connection to TWS drops or closes."""
        super().connectionClosed()
        logger.warning("Connection to TWS closed.")
        self._connected_event.clear()
        with self._lock:
            for event in self._request_events.values():
                event.set()

    def contractDetails(self, reqId: int, contractDetails: ContractDetails) -> None:
        """Callback receiving a single ContractDetails result."""
        super().contractDetails(reqId, contractDetails)
        with self._lock:
            if reqId not in self._request_events:
                logger.debug(
                    "Stale contractDetails callback ignored for unregistered reqId=%d",
                    reqId,
                )
                return
            if reqId not in self.contract_details_results:
                self.contract_details_results[reqId] = []
            self.contract_details_results[reqId].append(contractDetails)

        symbol = (
            contractDetails.contract.symbol if contractDetails.contract else "UNKNOWN"
        )
        con_id = contractDetails.contract.conId if contractDetails.contract else 0
        logger.info(
            "Received contract details for reqId=%d (%s, conId=%d)",
            reqId,
            symbol,
            con_id,
        )

    def contractDetailsEnd(self, reqId: int) -> None:
        """Callback indicating contract details transmission is complete for reqId."""
        super().contractDetailsEnd(reqId)
        with self._lock:
            if reqId in self._request_events:
                self._request_events[reqId].set()
                logger.info("Completed contract details request for reqId=%d", reqId)
            else:
                logger.debug(
                    "Stale contractDetailsEnd callback ignored for unregistered reqId=%d",
                    reqId,
                )

    def connect_and_run(
        self, host: str, port: int, client_id: int, timeout: float = 10.0
    ) -> bool:
        """Connect to TWS socket and start reader thread."""
        logger.info(
            "Connecting to TWS at %s:%d with client_id=%d...", host, port, client_id
        )
        try:
            self.connect(host, port, client_id)
        except OSError as e:
            logger.error("Failed to connect to TWS socket: %s", e)
            return False

        self._thread = threading.Thread(
            target=self.run, name="InstrumentDiscoveryThread", daemon=True
        )
        self._thread.start()

        if self._connected_event.wait(timeout=timeout):
            logger.info("Successfully connected to TWS.")
            return True
        else:
            logger.error("Timed out waiting for connection handshake (%.1fs).", timeout)
            self.disconnect_clean()
            return False

    def disconnect_clean(self) -> None:
        """Disconnect cleanly from TWS and wait for reader thread to exit."""
        logger.info("Disconnecting from TWS...")
        try:
            self.disconnect()
        except Exception as e:  # noqa: BLE001
            logger.warning("Error during disconnect: %s", e)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("Disconnected cleanly.")

    def register_request(self, req_id: int) -> threading.Event:
        """Register a request ID and return its completion Event."""
        with self._lock:
            event = threading.Event()
            self._request_events[req_id] = event
            self.contract_details_results[req_id] = []
            return event

    def unregister_request(self, req_id: int) -> None:
        """Unregister a request ID to isolate handlers from stale late-arriving callbacks."""
        with self._lock:
            self._request_events.pop(req_id, None)
            self.contract_details_results.pop(req_id, None)
            self.request_errors.pop(req_id, None)


def create_stk_contract(
    symbol: str, sec_type: str = "STK", exchange: str = "SMART", currency: str = "USD"
) -> Contract:
    """Helper to construct an IBKR stock/ETF/warrant contract specification."""
    contract = Contract()
    contract.symbol = symbol
    contract.secType = sec_type
    contract.exchange = exchange
    contract.currency = currency
    return contract


def create_contract_for_seed(
    seed: SeedRecord,
    sec_type: str | None = None,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Contract:
    """Helper to construct an IBKR contract specification for a SeedRecord."""
    if sec_type is None:
        # Default primary request to "STK" for all US equity seeds (including warrants/rights/units)
        sec_type = "STK"

    contract = Contract()
    contract.secType = sec_type
    contract.exchange = exchange
    contract.currency = currency

    if sec_type == "WAR":
        # Warrant fallback: extract 4-letter underlying ticker if 5-letter ticker in WARRANT, RIGHT, or UNIT category
        category = getattr(seed, "category", "")
        if len(seed.symbol) == 5 and category in ("WARRANT", "RIGHT", "UNIT"):
            underlying = seed.symbol[:-1]
        else:
            underlying = seed.symbol
        contract.symbol = underlying
        contract.localSymbol = seed.symbol
    else:
        contract.symbol = seed.symbol

    return contract


def extract_contract_record(
    details: ContractDetails,
    retrieved_at: str,
    seed: SeedRecord | None = None,
    status: str = "RESOLVED",
    error_code: str = "",
    error_message: str = "",
) -> dict[str, str]:
    """Convert an IBKR ContractDetails instance into a dictionary matching CSV schema."""
    c = details.contract if details else None

    def safe_str(val: Any) -> str:
        if val is None or val == 0 or val == "" or val == "0":
            return ""
        return str(val).strip()

    return {
        "seed_symbol": seed.symbol if seed else "",
        "seed_raw_symbol": seed.raw_symbol if seed else "",
        "seed_security_name": seed.security_name if seed else "",
        "seed_category": getattr(seed, "category", "COMMON_STOCK") if seed else "",
        "seed_exchange": seed.listing_exchange if seed else "",
        "seed_is_etf": str(seed.is_etf) if seed else "",
        "seed_source_file": seed.source_file if seed else "",
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
        "con_id": str(c.conId) if c and c.conId else "",
        "symbol": c.symbol if c and c.symbol else "",
        "local_symbol": safe_str(c.localSymbol),
        "sec_type": safe_str(c.secType),
        "exchange": safe_str(c.exchange),
        "primary_exchange": safe_str(c.primaryExchange),
        "currency": safe_str(c.currency),
        "trading_class": safe_str(c.tradingClass),
        "multiplier": safe_str(c.multiplier),
        "expiry": safe_str(c.lastTradeDateOrContractMonth),
        "strike": str(c.strike) if c and c.strike else "",
        "right": safe_str(c.right),
        "min_tick": str(details.minTick)
        if details and details.minTick is not None
        else "",
        "trading_hours": safe_str(details.tradingHours),
        "liquid_hours": safe_str(details.liquidHours),
        "time_zone_id": safe_str(details.timeZoneId),
        "underlying_con_id": str(details.underConId)
        if details and getattr(details, "underConId", None)
        else "",
        "description": details.longName or details.marketName or "",
        "retrieved_at": retrieved_at,
    }


def create_unresolved_record(
    seed: SeedRecord,
    status: str,
    error_code: str,
    error_message: str,
    retrieved_at: str,
) -> dict[str, str]:
    """Create an auditable CSV row for a seed that did not resolve to an IBKR contract."""
    return {
        "seed_symbol": seed.symbol,
        "seed_raw_symbol": seed.raw_symbol,
        "seed_security_name": seed.security_name,
        "seed_category": getattr(seed, "category", "COMMON_STOCK"),
        "seed_exchange": seed.listing_exchange,
        "seed_is_etf": str(seed.is_etf),
        "seed_source_file": seed.source_file,
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
        "con_id": "",
        "symbol": "",
        "local_symbol": "",
        "sec_type": "",
        "exchange": "",
        "primary_exchange": "",
        "currency": "",
        "trading_class": "",
        "multiplier": "",
        "expiry": "",
        "strike": "",
        "right": "",
        "min_tick": "",
        "trading_hours": "",
        "liquid_hours": "",
        "time_zone_id": "",
        "underlying_con_id": "",
        "description": "",
        "retrieved_at": retrieved_at,
    }


def compute_seed_key(seed: SeedRecord) -> str:
    """Generate deterministic seed identity key."""
    return f"{seed.source_file}:{seed.raw_symbol}:{getattr(seed, 'category', 'COMMON_STOCK')}"


def save_checkpoint(checkpoint_path: Path, data: dict[str, Any]) -> None:
    """Atomically save checkpoint data to JSON file via temporary file replacement."""
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = checkpoint_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp_path.replace(checkpoint_path)


def load_checkpoint(checkpoint_path: Path) -> dict[str, Any] | None:
    """Load existing checkpoint file if valid."""
    if not checkpoint_path.exists():
        return None
    try:
        with checkpoint_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to parse checkpoint file %s: %s", checkpoint_path, e)
        return None


def calculate_percentile(values: list[float], percentile: float) -> float:
    """Calculate percentile from a list of float values."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (percentile / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_vals):
        return sorted_vals[-1]
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return d0 + d1


def print_run_summary(
    status: str,
    seed_universe_count: int,
    completed_seeds_count: int,
    resolved_count: int,
    unresolved_count: int,
    error_count: int,
    timeout_count: int,
    total_contracts: int,
    multi_contract_seeds: int,
    total_requests: int,
    total_retries: int,
    rate_limit_hz: float,
    start_time: float,
    metrics: list[RequestMetric] | None = None,
    final_csv_path: Path | None = None,
    checkpoint_path: Path | None = None,
) -> None:
    """Format and log an auditable ASCII summary table of the discovery run."""
    duration_sec = time.monotonic() - start_time
    mins, secs = divmod(int(duration_sec), 60)
    duration_str = f"{mins}m {secs}s" if mins > 0 else f"{secs:.1f}s"

    actual_rps = (total_requests / duration_sec) if duration_sec > 0 else 0.0

    metrics_list = metrics or []
    latencies = [m.total_latency_ms for m in metrics_list]
    pacer_waits = [m.pacer_wait_ms for m in metrics_list]
    rtt_waits = [m.tws_rtt_ms for m in metrics_list]

    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    med_lat = calculate_percentile(latencies, 50.0)
    p95_lat = calculate_percentile(latencies, 95.0)
    p99_lat = calculate_percentile(latencies, 99.0)
    min_lat = min(latencies) if latencies else 0.0
    max_lat = max(latencies) if latencies else 0.0

    stk_succ = [
        m.total_latency_ms
        for m in metrics_list
        if m.sec_type == "STK" and m.status == "RESOLVED"
    ]
    war_prim = [
        m.total_latency_ms
        for m in metrics_list
        if m.sec_type == "WAR" and not m.is_fallback
    ]
    war_fall = [
        m.total_latency_ms
        for m in metrics_list
        if m.sec_type == "WAR" and m.is_fallback
    ]
    err_200 = [m.total_latency_ms for m in metrics_list if m.error_code == "200"]

    avg_stk_succ = sum(stk_succ) / len(stk_succ) if stk_succ else 0.0
    avg_war_prim = sum(war_prim) / len(war_prim) if war_prim else 0.0
    avg_war_fall = sum(war_fall) / len(war_fall) if war_fall else 0.0
    avg_err_200 = sum(err_200) / len(err_200) if err_200 else 0.0

    num_fallbacks = sum(1 for m in metrics_list if m.is_fallback)
    num_err_200 = sum(1 for m in metrics_list if m.error_code == "200")
    total_pacer_ms = sum(pacer_waits)
    total_rtt_ms = sum(rtt_waits)

    summary = [
        "==================================================",
        "IBKR INSTRUMENT MASTER DISCOVERY SUMMARY",
        "==================================================",
        f"Run Status         : {status}",
        f"Seed Universe      : {seed_universe_count:,} total seeds ({completed_seeds_count:,} processed)",
        "",
        "Audit Results:",
        f"  - Resolved       : {resolved_count:,}",
        f"  - Unresolved     : {unresolved_count:,}",
        f"  - Errors         : {error_count:,}",
        f"  - Timeouts       : {timeout_count:,}",
        "",
        "Contracts:",
        f"  - Total contracts: {total_contracts:,}",
        f"  - Multi-contract : {multi_contract_seeds:,} seeds",
        "",
        "Execution & Latency Metrics:",
        f"  - Total Requests : {total_requests:,} ({num_fallbacks:,} fallbacks, {num_err_200:,} Err200)",
        f"  - Throughput     : {actual_rps:.2f} req/sec (Limit: {rate_limit_hz:.1f} req/sec)",
        f"  - Retries        : {total_retries:,}",
        f"  - Duration       : {duration_str}",
        "",
        "Latency Distribution (ms):",
        f"  - Min / Max      : {min_lat:.1f} ms / {max_lat:.1f} ms",
        f"  - Avg / Median   : {avg_lat:.1f} ms / {med_lat:.1f} ms",
        f"  - P95 / P99      : {p95_lat:.1f} ms / {p99_lat:.1f} ms",
        "",
        "Category Breakdown Averages (ms):",
        f"  - STK Resolved   : {avg_stk_succ:.1f} ms ({len(stk_succ):,} reqs)",
        f"  - WAR Primary    : {avg_war_prim:.1f} ms ({len(war_prim):,} reqs)",
        f"  - WAR Fallback   : {avg_war_fall:.1f} ms ({len(war_fall):,} reqs)",
        f"  - Error 200      : {avg_err_200:.1f} ms ({len(err_200):,} reqs)",
        "",
        "Wait Time Distribution:",
        f"  - Pacer Wait Time: {total_pacer_ms / 1000.0:.2f} s total",
        f"  - TWS Socket RTT : {total_rtt_ms / 1000.0:.2f} s total",
        "",
        "Artifacts:",
        f"  - Final CSV      : {final_csv_path if final_csv_path else 'None'}",
        f"  - Checkpoint     : {checkpoint_path if checkpoint_path else 'None'}",
        "==================================================",
    ]
    logger.info("\n%s", "\n".join(summary))


def write_contracts_to_csv(records: list[dict[str, str]], output_path: Path) -> None:
    """Write contract detail dictionaries to a CSV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(records)
    logger.info("Wrote %d audit records to %s", len(records), output_path)


def discover_instruments(
    seeds: list[str | SeedRecord],
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 99,
    request_timeout: float = 3.0,
    output_dir: Path | None = None,
    limit: int | None = None,
    rate_limit_hz: float = 20.0,
    checkpoint_interval: int = 50,
    resume: bool = False,
    fresh: bool = False,
    max_retries: int = 3,
    max_reconnect_attempts: int = 3,
) -> Path:
    """Execute production-safe contract discovery for specified seeds."""
    start_time = time.monotonic()

    if output_dir is None:
        script_dir = Path(__file__).resolve().parent
        output_dir = script_dir.parent.parent / "data" / "instrument_master"

    checkpoints_dir = output_dir / "checkpoints"

    # Normalize seeds to SeedRecord objects
    normalized_seeds: list[SeedRecord] = []
    for s in seeds:
        if isinstance(s, SeedRecord):
            normalized_seeds.append(s)
        else:
            normalized_seeds.append(
                SeedRecord(
                    symbol=str(s).strip(),
                    raw_symbol=str(s).strip(),
                    security_name="",
                    listing_exchange="SMART",
                    is_etf=False,
                    is_test_issue=False,
                    source_file="manual",
                    category="COMMON_STOCK",
                )
            )

    if limit is not None and limit > 0:
        logger.info(
            "Applying seed sample limit: %d of %d total seeds",
            limit,
            len(normalized_seeds),
        )
        normalized_seeds = normalized_seeds[:limit]

    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    output_path = output_dir / f"ibkr_instrument_master_{date_str}.csv"

    # Compute deterministic checkpoint key based on seed universe
    seed_hash = hashlib.sha256(
        f"{len(normalized_seeds)}:{[s.symbol for s in normalized_seeds[:5]]}".encode()
    ).hexdigest()[:10]
    checkpoint_path = checkpoints_dir / f"checkpoint_{date_str}_{seed_hash}.json"

    pacer = RatePacer(rate_limit_hz=rate_limit_hz)
    completed_seed_keys: set[str] = set()
    all_records: list[dict[str, str]] = []
    metrics_history: list[RequestMetric] = []
    total_requests = 0
    total_retries = 0

    if fresh and checkpoint_path.exists():
        logger.info("Fresh run requested. Removing previous checkpoint file...")
        try:
            checkpoint_path.unlink()
        except OSError as e:
            logger.warning("Could not delete old checkpoint file: %s", e)

    if resume and checkpoint_path.exists():
        cp_data = load_checkpoint(checkpoint_path)
        if cp_data and cp_data.get("universe_count") == len(normalized_seeds):
            completed_seed_keys = set(cp_data.get("completed_seed_keys", []))
            all_records = cp_data.get("audit_records", [])
            total_requests = cp_data.get("total_requests", 0)
            total_retries = cp_data.get("total_retries", 0)
            logger.info(
                "RESUMING run from checkpoint: %d of %d seeds completed.",
                len(completed_seed_keys),
                len(normalized_seeds),
            )
        else:
            logger.warning(
                "Checkpoint file incompatible with current universe. Starting fresh run."
            )

    client = InstrumentDiscoveryClient()

    if not client.connect_and_run(host=host, port=port, client_id=client_id):
        raise RuntimeError(
            f"Failed to connect to IBKR TWS at {host}:{port} with client_id={client_id}"
        )

    try:
        for idx, seed_item in enumerate(normalized_seeds, start=1):
            seed_key = compute_seed_key(seed_item)
            if seed_key in completed_seed_keys:
                continue

            symbol = seed_item.symbol
            # Primary attempt is STK for all US equity seeds
            primary_sec_type = "STK"

            for attempt in range(1, max_retries + 1):
                # Socket health check and reconnection attempt if needed
                if client.connection_error or not (
                    client._thread and client._thread.is_alive()
                ):
                    logger.warning("TWS socket connection interrupted. Reconnecting...")
                    reconnected = False
                    for recon_attempt in range(1, max_reconnect_attempts + 1):
                        client.disconnect_clean()
                        time.sleep(1.0 * recon_attempt)
                        if client.connect_and_run(
                            host=host, port=port, client_id=client_id
                        ):
                            logger.info("TWS reconnection successful.")
                            reconnected = True
                            break

                    if not reconnected:
                        # Save state and raise clean error
                        save_checkpoint(
                            checkpoint_path,
                            {
                                "universe_count": len(normalized_seeds),
                                "completed_seed_keys": list(completed_seed_keys),
                                "audit_records": all_records,
                                "total_requests": total_requests,
                                "total_retries": total_retries,
                                "updated_at": datetime.now(UTC).isoformat(),
                            },
                        )
                        resolved_c = sum(
                            1 for r in all_records if r.get("status") == "RESOLVED"
                        )
                        unresolved_c = sum(
                            1 for r in all_records if r.get("status") == "UNRESOLVED"
                        )
                        error_c = sum(
                            1 for r in all_records if r.get("status") == "ERROR"
                        )
                        timeout_c = sum(
                            1 for r in all_records if r.get("status") == "TIMEOUT"
                        )
                        print_run_summary(
                            status="INTERRUPTED (TWS Disconnected)",
                            seed_universe_count=len(normalized_seeds),
                            completed_seeds_count=len(completed_seed_keys),
                            resolved_count=resolved_c,
                            unresolved_count=unresolved_c,
                            error_count=error_c,
                            timeout_count=timeout_c,
                            total_contracts=len(all_records),
                            multi_contract_seeds=0,
                            total_requests=total_requests,
                            total_retries=total_retries,
                            rate_limit_hz=rate_limit_hz,
                            start_time=start_time,
                            metrics=metrics_history,
                            checkpoint_path=checkpoint_path,
                        )
                        raise RuntimeError(
                            "TWS connection lost and reconnection attempts failed."
                        )

                t0_pacer = time.monotonic()
                pacer.acquire()
                t1_pacer = time.monotonic()
                pacer_wait_ms = (t1_pacer - t0_pacer) * 1000.0

                req_id = total_requests + 1
                total_requests += 1
                contract = create_contract_for_seed(
                    seed_item, sec_type=primary_sec_type
                )

                if attempt > 1:
                    total_retries += 1
                    logger.info(
                        "Retrying seed %s (attempt %d/%d, reqId=%d)...",
                        symbol,
                        attempt,
                        max_retries,
                        req_id,
                    )
                else:
                    logger.info(
                        "Requesting contract details for %s (reqId=%d, secType=%s, category=%s)...",
                        symbol,
                        req_id,
                        primary_sec_type,
                        getattr(seed_item, "category", "COMMON_STOCK"),
                    )

                event = client.register_request(req_id)
                t0_req = time.monotonic()
                client.reqContractDetails(req_id, contract)

                timed_out = not event.wait(timeout=request_timeout)
                t1_req = time.monotonic()
                tws_rtt_ms = (t1_req - t0_req) * 1000.0
                total_lat_ms = (t1_req - t0_pacer) * 1000.0

                details_list = list(client.contract_details_results.get(req_id, []))
                request_error = client.request_errors.get(req_id)

                # Unregister req_id immediately to prevent late callbacks from polluting future state
                client.unregister_request(req_id)

                err_code_prim = ""
                if request_error and request_error.startswith("Code "):
                    err_code_prim = (
                        request_error.split(":", 1)[0].replace("Code ", "").strip()
                    )

                status_prim = (
                    "RESOLVED"
                    if details_list
                    else (
                        "UNRESOLVED"
                        if err_code_prim == "200"
                        else ("TIMEOUT" if timed_out else "ERROR")
                    )
                )

                metrics_history.append(
                    RequestMetric(
                        seed_symbol=symbol,
                        req_id=req_id,
                        sec_type=primary_sec_type,
                        is_fallback=False,
                        category=getattr(seed_item, "category", "COMMON_STOCK"),
                        status=status_prim,
                        error_code=err_code_prim,
                        pacer_wait_ms=pacer_wait_ms,
                        tws_rtt_ms=tws_rtt_ms,
                        total_latency_ms=total_lat_ms,
                        num_contracts=len(details_list),
                    )
                )

                # Fallback Retry for WARRANT/RIGHT/UNIT seeds if STK returned Error 200 or timed out
                if (
                    (not details_list)
                    and getattr(seed_item, "category", "")
                    in ("WARRANT", "RIGHT", "UNIT")
                    and (timed_out or (request_error and "Code 200" in request_error))
                ):
                    fallback_req_id = total_requests + 1
                    total_requests += 1

                    t0_fb_pacer = time.monotonic()
                    pacer.acquire()
                    t1_fb_pacer = time.monotonic()
                    fb_pacer_wait_ms = (t1_fb_pacer - t0_fb_pacer) * 1000.0

                    logger.info(
                        "Fallback retry for %s %s with secType=WAR (reqId=%d)...",
                        getattr(seed_item, "category", ""),
                        symbol,
                        fallback_req_id,
                    )
                    fb_contract = create_contract_for_seed(seed_item, sec_type="WAR")
                    fb_event = client.register_request(fallback_req_id)
                    t0_fb_req = time.monotonic()
                    client.reqContractDetails(fallback_req_id, fb_contract)

                    fb_timed_out = not fb_event.wait(timeout=request_timeout)
                    t1_fb_req = time.monotonic()
                    fb_tws_rtt_ms = (t1_fb_req - t0_fb_req) * 1000.0
                    fb_total_lat_ms = (t1_fb_req - t0_fb_pacer) * 1000.0

                    fb_details = list(
                        client.contract_details_results.get(fallback_req_id, [])
                    )
                    fb_error = client.request_errors.get(fallback_req_id)
                    client.unregister_request(fallback_req_id)

                    err_code_fb = ""
                    if fb_error and fb_error.startswith("Code "):
                        err_code_fb = (
                            fb_error.split(":", 1)[0].replace("Code ", "").strip()
                        )

                    status_fb = (
                        "RESOLVED"
                        if fb_details
                        else (
                            "UNRESOLVED"
                            if err_code_fb == "200"
                            else ("TIMEOUT" if fb_timed_out else "ERROR")
                        )
                    )

                    metrics_history.append(
                        RequestMetric(
                            seed_symbol=symbol,
                            req_id=fallback_req_id,
                            sec_type="WAR",
                            is_fallback=True,
                            category=getattr(seed_item, "category", "COMMON_STOCK"),
                            status=status_fb,
                            error_code=err_code_fb,
                            pacer_wait_ms=fb_pacer_wait_ms,
                            tws_rtt_ms=fb_tws_rtt_ms,
                            total_latency_ms=fb_total_lat_ms,
                            num_contracts=len(fb_details),
                        )
                    )

                    if fb_details:
                        details_list = fb_details
                        request_error = None
                        timed_out = False
                    else:
                        if fb_error:
                            request_error = fb_error
                        if fb_timed_out:
                            timed_out = True

                retrieved_at = datetime.now(UTC).isoformat()

                if details_list:
                    for d in details_list:
                        record = extract_contract_record(
                            d, retrieved_at, seed=seed_item, status="RESOLVED"
                        )
                        all_records.append(record)
                    break

                # Fast-Fail Permanent Error 200: Do NOT retry Error 200 repeatedly
                if request_error and "Code 200" in request_error:
                    logger.warning(
                        "UNRESOLVED seed %s (reqId=%d): Error 200 (No security definition found). Failing fast without retry.",
                        symbol,
                        req_id,
                    )
                    all_records.append(
                        create_unresolved_record(
                            seed=seed_item,
                            status="UNRESOLVED",
                            error_code="200",
                            error_message="No security definition has been found for the request",
                            retrieved_at=retrieved_at,
                        )
                    )
                    break

                if timed_out:
                    if attempt < max_retries:
                        backoff = 1.0 * (2 ** (attempt - 1))
                        logger.warning(
                            "Timeout on seed %s (attempt %d/%d). Backing off %.1fs...",
                            symbol,
                            attempt,
                            max_retries,
                            backoff,
                        )
                        time.sleep(backoff)
                        continue
                    else:
                        all_records.append(
                            create_unresolved_record(
                                seed=seed_item,
                                status="TIMEOUT",
                                error_code="",
                                error_message="Timed out waiting for contract details",
                                retrieved_at=retrieved_at,
                            )
                        )
                        break

                if request_error:
                    if attempt < max_retries:
                        backoff = 1.0 * (2 ** (attempt - 1))
                        logger.warning(
                            "Transient error on seed %s: %s (attempt %d/%d). Backing off %.1fs...",
                            symbol,
                            request_error,
                            attempt,
                            max_retries,
                            backoff,
                        )
                        time.sleep(backoff)
                        continue
                    else:
                        err_code_str = ""
                        err_msg_str = request_error
                        if request_error.startswith("Code "):
                            parts = request_error.split(":", 1)
                            err_code_str = parts[0].replace("Code ", "").strip()
                            if len(parts) > 1:
                                err_msg_str = parts[1].strip()

                        all_records.append(
                            create_unresolved_record(
                                seed=seed_item,
                                status="ERROR",
                                error_code=err_code_str,
                                error_message=err_msg_str,
                                retrieved_at=retrieved_at,
                            )
                        )
                        break

            completed_seed_keys.add(seed_key)

            # Save checkpoint periodically
            if idx % checkpoint_interval == 0:
                save_checkpoint(
                    checkpoint_path,
                    {
                        "universe_count": len(normalized_seeds),
                        "completed_seed_keys": list(completed_seed_keys),
                        "audit_records": all_records,
                        "total_requests": total_requests,
                        "total_retries": total_retries,
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                )

    finally:
        client.disconnect_clean()

    # Save final checkpoint and CSV output
    save_checkpoint(
        checkpoint_path,
        {
            "universe_count": len(normalized_seeds),
            "completed_seed_keys": list(completed_seed_keys),
            "audit_records": all_records,
            "total_requests": total_requests,
            "total_retries": total_retries,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )

    write_contracts_to_csv(all_records, output_path)

    # Compute summary counts
    resolved_count = sum(1 for r in all_records if r.get("status") == "RESOLVED")
    unresolved_count = sum(1 for r in all_records if r.get("status") == "UNRESOLVED")
    error_count = sum(1 for r in all_records if r.get("status") == "ERROR")
    timeout_count = sum(1 for r in all_records if r.get("status") == "TIMEOUT")

    # Multi-contract seeds
    seed_symbol_counts: dict[str, int] = {}
    for r in all_records:
        if r.get("status") == "RESOLVED":
            sym = r.get("seed_symbol", "")
            seed_symbol_counts[sym] = seed_symbol_counts.get(sym, 0) + 1
    multi_contract_seeds = sum(1 for c in seed_symbol_counts.values() if c > 1)

    print_run_summary(
        status="COMPLETED",
        seed_universe_count=len(normalized_seeds),
        completed_seeds_count=len(completed_seed_keys),
        resolved_count=resolved_count,
        unresolved_count=unresolved_count,
        error_count=error_count,
        timeout_count=timeout_count,
        total_contracts=resolved_count,
        multi_contract_seeds=multi_contract_seeds,
        total_requests=total_requests,
        total_retries=total_retries,
        rate_limit_hz=rate_limit_hz,
        start_time=start_time,
        metrics=metrics_history,
        final_csv_path=output_path,
        checkpoint_path=checkpoint_path,
    )

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="IBKR Instrument Master discovery script (Read-Only)."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="IBKR TWS host IP (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7497,
        help="IBKR TWS API socket port (default: 7497)",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=99,
        help="Dedicated API Client ID (default: 99)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Specific seed symbols to discover (e.g. AAPL MSFT NVDA TSLA SPY)",
    )
    parser.add_argument(
        "--use-nasdaq-seeds",
        action="store_true",
        help="Fetch seed universe dynamically from NASDAQ Trader directory",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of seed symbols to discover (default: 25 when using NASDAQ seeds)",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=20.0,
        help="Rate limit in requests per second (default: 20.0)",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=50,
        help="Save checkpoint after processing N seeds (default: 50)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume discovery from existing checkpoint file if available",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Start a fresh run, ignoring existing checkpoint files",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum retries for transient errors/timeouts (default: 3)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Timeout in seconds per contract request (default: 3.0)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose log output"
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    logger.info("Starting IBKR Instrument Master discovery pipeline...")

    seeds: list[str | SeedRecord]
    if args.use_nasdaq_seeds:
        logger.info("Fetching seed universe from NASDAQ Trader directory...")
        seeds = fetch_nasdaq_seed_universe()
        if args.limit is None:
            args.limit = 25
    elif args.symbols:
        seeds = args.symbols
    else:
        seeds = DEFAULT_SEED_SYMBOLS

    logger.info("Seed pool size: %d items", len(seeds))

    try:
        csv_path = discover_instruments(
            seeds=seeds,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            request_timeout=args.timeout,
            limit=args.limit,
            rate_limit_hz=args.rate_limit,
            checkpoint_interval=args.checkpoint_interval,
            resume=args.resume,
            fresh=args.fresh,
            max_retries=args.max_retries,
        )
        logger.info("SUCCESS: Discovery output written to %s", csv_path)
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        logger.error("FAILURE: Discovery process failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
