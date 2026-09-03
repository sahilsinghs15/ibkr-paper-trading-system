"""Validate candidate universe against real Paper IB Gateway :4002.

Dedicated prototype client — NEVER port 4001.
Per-instrument reqContractDetails (STK for ETF, CFD for CFD).
Only VERIFIED when IBKR returns valid conId.
Suitable for deriving the final 500+500 margin-benchmark universe.

Usage:
  PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.validate_universe --port 4002 --limit 20
  PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.validate_universe --port 4002 --rate 4
"""

from __future__ import annotations
import argparse
import csv
import json
import time
import threading
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("validate_universe")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Validate config
PROTOTYPE_PORT = 4002
FORBIDDEN_PORT = 4001

try:
    from ibapi.client import EClient as IB_EClient
    from ibapi.wrapper import EWrapper as IB_EWrapper
    from ibapi.contract import Contract as IB_Contract
except ImportError:
    IB_EClient = object  # type: ignore
    IB_EWrapper = object  # type: ignore
    IB_Contract = object  # type: ignore


class ValidatorWrapper(IB_EWrapper):
    def __init__(self):
        IB_EWrapper.__init__(self)
        self._connected = threading.Event()
        self.next_order_id: int | None = None
        self._lock = threading.Lock()
        self._events: dict[int, threading.Event] = {}
        self._results: dict[int, list[Any]] = {}
        self._errors: dict[int, str] = {}
        self._next_req = 80000

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self._connected.set()
        logger.info("nextValidId %d", orderId)

    def error(self, reqId:int, errorCode:int, errorString:str, advancedOrderRejectJson:str=""):
        if 2000 <= errorCode < 3000:
            logger.debug("Info %d reqId=%d %s", errorCode, reqId, errorString)
            return
        logger.warning("IBKR error reqId=%d code=%d %s", reqId, errorCode, errorString)
        with self._lock:
            if reqId in self._events:
                self._errors[reqId] = f"Code {errorCode}: {errorString}"
                self._events[reqId].set()

    def contractDetails(self, reqId:int, contractDetails:Any):
        super().contractDetails(reqId, contractDetails)
        with self._lock:
            if reqId in self._results:
                self._results[reqId].append(contractDetails)

    def contractDetailsEnd(self, reqId:int):
        super().contractDetailsEnd(reqId)
        with self._lock:
            evt = self._events.get(reqId)
            if evt:
                evt.set()


class ValidatorClient(IB_EWrapper, IB_EClient):
    def __init__(self):
        IB_EWrapper.__init__(self)
        IB_EClient.__init__(self, wrapper=self)
        self._connected = threading.Event()
        self.next_order_id: int | None = None
        self._lock = threading.Lock()
        self._events: dict[int, threading.Event] = {}
        self._results: dict[int, list[Any]] = {}
        self._errors: dict[int, str] = {}
        self._next_req = 80000

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self._connected.set()
        logger.info("nextValidId %d", orderId)

    def error(self, reqId:int, errorCode:int, errorString:str, advancedOrderRejectJson:str=""):
        if 2000 <= errorCode < 3000:
            logger.debug("Info %d reqId=%d %s", errorCode, reqId, errorString)
            return
        logger.warning("IBKR error reqId=%d code=%d %s", reqId, errorCode, errorString)
        with self._lock:
            if reqId in self._events:
                self._errors[reqId] = f"Code {errorCode}: {errorString}"
                self._events[reqId].set()

    def contractDetails(self, reqId:int, contractDetails:Any):
        super().contractDetails(reqId, contractDetails)
        with self._lock:
            if reqId in self._results:
                self._results[reqId].append(contractDetails)

    def contractDetailsEnd(self, reqId:int):
        super().contractDetailsEnd(reqId)
        with self._lock:
            evt = self._events.get(reqId)
            if evt:
                evt.set()


def build_contract(row: dict[str,str]):
    c = IB_Contract()
    c.symbol = row["symbol"].strip().upper()
    if row["instrument_type"].strip().upper() == "CFD":
        c.secType = "CFD"
    else:
        c.secType = "STK"
    # For prototype validation we use SMART/USD per resolver.py:44 + cfd_discover.py:19
    # Exchange from CSV is ARCA/AMEX/SMART but IBKR discovery uses SMART for lookup.
    c.exchange = "SMART"
    c.currency = "USD"
    return c


def pick_details(details: list[Any], instrument_type: str) -> tuple[Any | None, str]:
    if not details:
        return None, "NO_DETAILS"
    wanted = "CFD" if instrument_type.upper()=="CFD" else "STK"
    filtered=[d for d in details if getattr(getattr(d,"contract",None),"secType","").upper()==wanted]
    if not filtered:
        return None, f"WRONG_SECTYPE expected {wanted}"
    # For CFD need unique SMART USD (cfd_discover.py:40)
    if wanted=="CFD":
        smart=[d for d in filtered if getattr(getattr(d,"contract",None),"exchange","").upper()=="SMART" and getattr(getattr(d,"contract",None),"currency","").upper()=="USD"]
        pool = smart if smart else filtered
        if len(pool)!=1:
            # If multiple, take first but note ambiguous
            if len(pool)>1:
                return None, f"AMBIGUOUS_CFD {len(pool)} matches"
            return None, "NO_CFD_MATCH"
        return pool[0], "OK"
    # For STK/ETF: prefer USD SMART with conId
    # Take first with conId
    for d in filtered:
        con=int(getattr(getattr(d,"contract",None),"conId",0) or 0)
        if con>0:
            return d, "OK"
    return filtered[0], "OK"


def main():
    ap = argparse.ArgumentParser(description="Validate candidate universe via Paper Gateway :4002")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=99)
    ap.add_argument("--rate", type=float, default=4.0, help="requests/sec pacing")
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--input", type=str, default="data/instruments_candidates.csv")
    ap.add_argument("--output-verified", type=str, default="data/instruments_verified.csv")
    ap.add_argument("--output-full", type=str, default="data/instruments_validation_full.csv")
    ap.add_argument("--dry-run", action="store_true", help="Do not connect — mark all as NOT_VERIFIED locally")
    args = ap.parse_args()

    if args.port == FORBIDDEN_PORT:
        raise SystemExit(f"Refusing port {FORBIDDEN_PORT} (live). Use {PROTOTYPE_PORT} paper.")

    base = Path(__file__).resolve().parent.parent
    cand_path = Path(args.input) if Path(args.input).is_absolute() else base / args.input
    if not cand_path.exists():
        raise SystemExit(f"Candidates CSV not found: {cand_path}")

    import csv as csvm
    rows = list(csvm.DictReader(open(cand_path)))
    if args.limit:
        rows = rows[:args.limit]
    print(f"Validating {len(rows)} candidates from {cand_path} against {args.host}:{args.port} clientId={args.client_id} rate={args.rate}/s timeout={args.timeout}s")

    # rate pacing: token bucket simple sleep
    min_interval = 1.0/args.rate if args.rate>0 else 0
    last_req = 0.0

    if args.dry_run:
        print("DRY RUN — not connecting to IBKR. All rows marked NOT_VERIFIED (no gateway).")
        full_rows=[]
        for r in rows:
            full_rows.append({
                "symbol": r["symbol"],
                "instrument_type": r["instrument_type"],
                "exchange": r["exchange"],
                "currency": r["currency"],
                "conId": "",
                "primary_exchange": "",
                "validation_status": "NOT_VERIFIED_NO_GATEWAY",
                "error": "dry-run: no IBKR connection attempted",
                "timestamp_utc": datetime.now(UTC).isoformat(),
            })
        # write outputs
        out_full = base / args.output_full if not Path(args.output_full).is_absolute() else Path(args.output_full)
        out_full.parent.mkdir(parents=True, exist_ok=True)
        with out_full.open("w", newline="") as f:
            w=csvm.DictWriter(f, fieldnames=["symbol","instrument_type","exchange","currency","conId","primary_exchange","validation_status","error","timestamp_utc"])
            w.writeheader()
            w.writerows(full_rows)
        out_verified = base / args.output_verified if not Path(args.output_verified).is_absolute() else Path(args.output_verified)
        out_verified.parent.mkdir(parents=True, exist_ok=True)
        with out_verified.open("w", newline="") as f:
            w=csvm.DictWriter(f, fieldnames=["instrument_type","symbol","exchange","currency","conId","primary_exchange","validation_status","timestamp_utc"])
            w.writeheader()
        # report
        print(json.dumps({
            "etf_candidates": sum(1 for r in rows if r["instrument_type"]=="ETF"),
            "cfd_candidates": sum(1 for r in rows if r["instrument_type"]=="CFD"),
            "etf_verified": 0,
            "cfd_verified": 0,
            "total_verified": 0,
            "failures": len(rows),
            "note": "Dry run — gateway not contacted locally"
        }, indent=2))
        return

    # Real connect
    client = ValidatorClient()
    try:
        client.connect(args.host, args.port, args.client_id)
    except Exception as e:
        print(f"TCP connect failed: {e}")
        # write full as unreachable
        out_full = base / args.output_full if not Path(args.output_full).is_absolute() else Path(args.output_full)
        out_full.parent.mkdir(parents=True, exist_ok=True)
        with out_full.open("w", newline="") as f:
            w=csvm.DictWriter(f, fieldnames=["symbol","instrument_type","exchange","currency","conId","primary_exchange","validation_status","error","timestamp_utc"])
            w.writeheader()
            for r in rows:
                w.writerow({"symbol": r["symbol"], "instrument_type": r["instrument_type"], "exchange": r["exchange"], "currency": r["currency"], "conId":"", "primary_exchange":"", "validation_status":"IBKR_UNREACHABLE", "error": str(e)[:500], "timestamp_utc": datetime.now(UTC).isoformat()})
        out_verified = base / args.output_verified if not Path(args.output_verified).is_absolute() else Path(args.output_verified)
        with out_verified.open("w", newline="") as f:
            w=csvm.DictWriter(f, fieldnames=["instrument_type","symbol","exchange","currency","conId","primary_exchange","validation_status","timestamp_utc"])
            w.writeheader()
        raise SystemExit(1)

    t = threading.Thread(target=client.run, daemon=True, name="ValidatorThread")
    t.start()
    if not client._connected.wait(timeout=10):
        client.disconnect()
        print("Handshake timeout — Paper Gateway not reachable on :4002")
        # Write unreachable report for local-only phase
        out_full = base / args.output_full if not Path(args.output_full).is_absolute() else Path(args.output_full)
        out_full.parent.mkdir(parents=True, exist_ok=True)
        with out_full.open("w", newline="") as f:
            w=csvm.DictWriter(f, fieldnames=["symbol","instrument_type","exchange","currency","conId","primary_exchange","validation_status","error","timestamp_utc"])
            w.writeheader()
            for r in rows:
                w.writerow({"symbol": r["symbol"], "instrument_type": r["instrument_type"], "exchange": r["exchange"], "currency": r["currency"], "conId":"", "primary_exchange":"", "validation_status":"IBKR_UNREACHABLE", "error": "Paper Gateway :4002 not reachable locally (expected before EC2)", "timestamp_utc": datetime.now(UTC).isoformat()})
        out_verified = base / args.output_verified if not Path(args.output_verified).is_absolute() else Path(args.output_verified)
        out_verified.parent.mkdir(parents=True, exist_ok=True)
        with out_verified.open("w", newline="") as f:
            w=csvm.DictWriter(f, fieldnames=["instrument_type","symbol","exchange","currency","conId","primary_exchange","validation_status","timestamp_utc"])
            w.writeheader()
        report={
            "etf_candidates": sum(1 for r in rows if r["instrument_type"]=="ETF"),
            "etf_verified": 0,
            "cfd_candidates": sum(1 for r in rows if r["instrument_type"]=="CFD"),
            "cfd_verified": 0,
            "arca_etfs_verified": 0,
            "amex_etfs_verified": 0,
            "total_verified": 0,
            "synthetic_instruments": 0,
            "failures": len(rows),
            "candidates_file": str(cand_path),
            "verified_file": str(out_verified),
            "full_validation_file": str(out_full),
            "gateway": f"{args.host}:{args.port}",
            "client_id": args.client_id,
            "note": "Local validation attempted but Paper Gateway :4002 not reachable (expected — EC2 phase required for real verification)"
        }
        print(json.dumps(report, indent=2))
        (base/"data"/"validation_report.json").write_text(json.dumps(report, indent=2))
        raise SystemExit(0)
    print("Connected to Paper Gateway :4002")

    full_rows=[]
    verified_rows=[]
    failures=0

    for idx, r in enumerate(rows, start=1):
        # pacing
        now=time.monotonic()
        elapsed=now-last_req
        if elapsed < min_interval:
            time.sleep(min_interval-elapsed)
        last_req=time.monotonic()

        contract = build_contract(r)
        w = client
        with w._lock:
            req_id = w._next_req
            w._next_req+=1
            evt = threading.Event()
            w._events[req_id]=evt
            w._results[req_id]=[]
            w._errors.pop(req_id, None)
        client.reqContractDetails(req_id, contract)
        completed = evt.wait(timeout=args.timeout)
        with w._lock:
            details=list(w._results.get(req_id,[]))
            err=w._errors.get(req_id,"")
            w._events.pop(req_id,None)
            w._results.pop(req_id,None)
            w._errors.pop(req_id,None)

        ts=datetime.now(UTC).isoformat()
        if not completed:
            status="TIMEOUT"
            conId=""
            primary=""
            error=f"Timeout {args.timeout}s"
            failures+=1
        elif err:
            status="FAILED"
            conId=""
            primary=""
            error=err[:500]
            failures+=1
        else:
            picked, reason = pick_details(details, r["instrument_type"])
            if picked is None:
                status="FAILED"
                conId=""
                primary=""
                error=reason[:500]
                failures+=1
            else:
                c=getattr(picked,"contract",None)
                conId=str(int(getattr(c,"conId",0) or 0)) if c else ""
                primary=str(getattr(c,"primaryExchange","") or "")
                if conId and int(conId)>0:
                    status="VERIFIED"
                    error=""
                    verified_rows.append({
                        "instrument_type": r["instrument_type"],
                        "symbol": r["symbol"],
                        "exchange": r["exchange"],
                        "currency": r["currency"],
                        "conId": conId,
                        "primary_exchange": primary,
                        "validation_status": status,
                        "timestamp_utc": ts,
                    })
                else:
                    status="FAILED"
                    error="NO_CONID"
                    failures+=1
        full_rows.append({
            "symbol": r["symbol"],
            "instrument_type": r["instrument_type"],
            "exchange": r["exchange"],
            "currency": r["currency"],
            "conId": conId if 'conId' in locals() else "",
            "primary_exchange": primary if 'primary' in locals() else "",
            "validation_status": status,
            "error": error,
            "timestamp_utc": ts,
        })
        if idx%50==0 or idx==len(rows):
            print(f"[{idx}/{len(rows)}] {r['instrument_type']} {r['symbol']} -> {status} conId={conId if 'conId' in locals() else ''}")

    client.disconnect()
    try:
        client._connected.clear()
    except: pass
    if t.is_alive():
        t.join(timeout=2)

    # Write outputs
    out_full = base / args.output_full if not Path(args.output_full).is_absolute() else Path(args.output_full)
    out_full.parent.mkdir(parents=True, exist_ok=True)
    with out_full.open("w", newline="") as f:
        w=csvm.DictWriter(f, fieldnames=["symbol","instrument_type","exchange","currency","conId","primary_exchange","validation_status","error","timestamp_utc"])
        w.writeheader()
        w.writerows(full_rows)
    out_verified = base / args.output_verified if not Path(args.output_verified).is_absolute() else Path(args.output_verified)
    out_verified.parent.mkdir(parents=True, exist_ok=True)
    with out_verified.open("w", newline="") as f:
        w=csvm.DictWriter(f, fieldnames=["instrument_type","symbol","exchange","currency","conId","primary_exchange","validation_status","timestamp_utc"])
        w.writeheader()
        w.writerows(verified_rows)

    etf_cands=sum(1 for r in rows if r["instrument_type"]=="ETF")
    cfd_cands=sum(1 for r in rows if r["instrument_type"]=="CFD")
    etf_ver=sum(1 for r in verified_rows if r["instrument_type"]=="ETF")
    cfd_ver=sum(1 for r in verified_rows if r["instrument_type"]=="CFD")
    arca_ver=sum(1 for r in verified_rows if r["instrument_type"]=="ETF" and r["exchange"]=="ARCA")
    amex_ver=sum(1 for r in verified_rows if r["instrument_type"]=="ETF" and r["exchange"]=="AMEX")

    report={
        "etf_candidates": etf_cands,
        "etf_verified": etf_ver,
        "cfd_candidates": cfd_cands,
        "cfd_verified": cfd_ver,
        "arca_etfs_verified": arca_ver,
        "amex_etfs_verified": amex_ver,
        "total_verified": len(verified_rows),
        "synthetic_instruments": 0,
        "failures": failures,
        "candidates_file": str(cand_path),
        "verified_file": str(out_verified),
        "full_validation_file": str(out_full),
        "gateway": f"{args.host}:{args.port}",
        "client_id": args.client_id,
    }
    print(json.dumps(report, indent=2))
    (base/"data"/"validation_report.json").write_text(json.dumps(report, indent=2))

if __name__=="__main__":
    main()
