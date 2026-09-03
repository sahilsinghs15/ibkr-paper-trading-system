"""Generate 500 ETFs (ARCA/AMEX) + 500 CFDs CSV.

Uses real known tickers where possible; remainder synthesized.
Clearly marks that conId/margins are NOT in this CSV — they are fetched via IBKR.
"""

from __future__ import annotations

import csv
from pathlib import Path

# Real popular ETFs — mix ARCA and AMEX
REAL_ETFS_ARCA = [
    "SPY","QQQ","IWM","DIA","VOO","VTI","EFA","EEM","AGG","LQD","HYG","XLF","XLE","XLK","XLV","XLI",
    "XLP","XLY","XLB","XLU","GLD","SLV","USO","UNG","TLT","IEF","SHY","IYR","VNQ","ARKK","SMH","SOXX",
    "XBI","IBB","KRE","KBE","GDX","GDXJ","SIL","SLX","COPX","REMX","URA","LIT","ICLN","TAN","QCLN",
    "PBW","FAN","PBD","WOOD","CUT","MOO","WEAT","CORN","SOYB","DBA","DBC","PDBC","GSG","USCI","DJP",
    "TIP","VTIP","BND","BSV","BIV","BLV","VGSH","VGIT","VGLT","SCHO","SCHR","SCHQ","GOVT","TFLO","SHV",
    "BIL","SGOV","USFR","MINT","NEAR","JPST","SHYG","JNK","BKLN","SRLN","EMB","PCY","VWOB","IGOV","BWX",
    "BNDX","EMLC","LEMB","ELD","ISHG","IGIB","IUSB","FBND","ANGL","HYLB","USHY","JCPB","LQDH","HYS",
    "XYLD","QYLD","JEPI","JEPQ","SCHD","DGRO","DGRW","VIG","NOBL","DIV","DVY","HDV","SPYD","SDY","VYM",
    "SPHD","SPYV","SPYG","VOOG","VOOV","MGK","MGV","VUG","VTV","VV","VO","VB","VOT","VBK","VTWO","VTWV",
]

REAL_ETFS_AMEX = [
    "SPY","IWM","DIA","XLF","XLE","XLK","XLV","XLI","XLP","XLY","XLB","XLU","EWZ","EWJ","EWG","EWU",
    "EWA","EWH","EWI","EWL","EWP","EWQ","EWS","EWW","EWT","EWY","EIDO","EIRL","EIS","ENZL","EPHE","EPI",
    "EPP","EPOL","EPU","ERUS","EUSA","EWD","EWK","EWN","EWO","EWM","EWS","EWU","EWW","FXI","GXC","MCHI",
    "ASHR","ASHS","CNYA","KWEB","PGJ","CHIQ","CHIX","CHII","CHIE","CHIM","TAO","XPP","PEK","GCH",
]

def generate():
    base = Path(__file__).resolve().parent
    out = base / "instruments.csv"

    # Dedupe and build 500 ETFs
    etfs: list[tuple[str,str]] = []
    seen = set()
    # Alternate ARCA / AMEX for variety, prefer ARCA for most
    # Fill ARCA first
    for sym in REAL_ETFS_ARCA:
        if len(etfs) >= 300:
            break
        if sym not in seen:
            etfs.append((sym, "ARCA"))
            seen.add(sym)
    for sym in REAL_ETFS_AMEX:
        if len(etfs) >= 350:
            break
        if sym not in seen:
            etfs.append((sym, "AMEX"))
            seen.add(sym)
    # Synthesize remainder as ETF_XXX ARCA/AMEX
    idx = 1
    while len(etfs) < 500:
        sym = f"ETF{idx:03d}"
        ex = "ARCA" if idx % 2 == 0 else "AMEX"
        if sym not in seen:
            etfs.append((sym, ex))
            seen.add(sym)
        idx += 1

    # CFDs: use common US stock symbols as CFD underlying (IBKR CFD on SMART/USD)
    REAL_CFD_SYMBOLS = [
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","BRK.B","JPM","JNJ","V","PG","UNH","HD","BAC",
        "MA","DIS","PYPL","ADBE","CRM","NFLX","INTC","CSCO","PFE","KO","PEP","WMT","MRK","ABT","T",
        "VZ","ORCL","AVGO","QCOM","TXN","COST","NKE","MCD","LLY","TMO","DHR","BMY","ABBV","CVX","XOM",
        "NEE","DUK","SO","D","AEP","EXC","SRE","PEG","ED","XEL","WEC","ES","FE","PPL","AEE","CMS","ETR",
        "NI","LNT","A","AWK","CNP","DTE","EIX","PCG","Sempra","WEC","XEL","AES","NRG","VST","CEG","EO",
        "GOLD","NEM","AEM","KGC","GOLD","WPM","FNV","RGLD","PAAS","AG","HL","CDE","EXK","SVM","MAG","FSM",
    ]
    # Ensure truncated
    REAL_CFD_SYMBOLS = [s.replace(".","") for s in REAL_CFD_SYMBOLS]

    cfds: list[tuple[str,str]] = []
    seen_cfd = set()
    for sym in REAL_CFD_SYMBOLS:
        if len(cfds) >= 250:
            break
        clean = sym.strip().upper().replace(".","")
        if clean and clean not in seen_cfd:
            cfds.append((clean, "SMART"))
            seen_cfd.add(clean)
    idx = 1
    while len(cfds) < 500:
        sym = f"CFD{idx:03d}"
        if sym not in seen_cfd:
            cfds.append((sym, "SMART"))
            seen_cfd.add(sym)
        idx += 1

    # Write CSV
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["instrument_type","symbol","exchange","currency"])
        w.writeheader()
        for sym, ex in etfs:
            w.writerow({"instrument_type": "ETF", "symbol": sym, "exchange": ex, "currency": "USD"})
        for sym, ex in cfds:
            w.writerow({"instrument_type": "CFD", "symbol": sym, "exchange": ex, "currency": "USD"})

    print(f"Wrote {len(etfs)} ETFs + {len(cfds)} CFDs = {len(etfs)+len(cfds)} to {out}")
    # Also small sample for dev
    sample = base / "instruments_sample_20.csv"
    with sample.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["instrument_type","symbol","exchange","currency"])
        w.writeheader()
        # 10 ETF + 10 CFD
        for sym, ex in etfs[:10]:
            w.writerow({"instrument_type": "ETF", "symbol": sym, "exchange": ex, "currency": "USD"})
        for sym, ex in cfds[:10]:
            w.writerow({"instrument_type": "CFD", "symbol": sym, "exchange": ex, "currency": "USD"})
    print(f"Sample -> {sample}")

if __name__ == "__main__":
    generate()
