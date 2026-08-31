# Production MFT Load Testing & Performance Verification

This document specifies the burst load testing methodology, environment setup, test scripts, and key success metrics.

---

## 1. Load Test Objectives & Scenario Specifications

The load test verifies that the system can process large signal bursts without:
- Holding HTTP webhook connections open waiting for fills.
- Exceeding target HTTP ingestion latency limits.
- Creating duplicate execution intents.
- Saturating the IBKR TWS gateway socket or dropping socket messages.

### Test Scenarios:

1. **Scenario 1: 300 Signal Burst**
   - 300 unique TradingView alert JSON webhooks sent concurrently within a 1-second window.
2. **Scenario 2: 500 Signal Burst Stress Test**
   - 500 unique TradingView alert JSON webhooks sent concurrently within a 1-second window.
3. **Scenario 3: 100x Duplicate Signal Burst**
   - 100 identical TradingView webhooks (same `strategy_id` and `signal_id`) sent simultaneously to verify atomic idempotency.

---

## 2. Key Performance Indicators (KPIs) & Target Metrics

| Metric | Target / Threshold |
|---|---|
| **Webhook Ingestion Latency (p50)** | < 5ms |
| **Webhook Ingestion Latency (p95)** | < 15ms |
| **Webhook Ingestion Latency (p99)** | < 30ms |
| **Maximum Webhook Response Latency** | < 100ms (Must NOT wait 90s for fills!) |
| **Signal Ingestion HTTP Status** | 100% HTTP 202 Accepted |
| **Duplicate Intent Creation Rate** | 0.0% (Exactly 1 job created per unique signal) |
| **Worker Queue Throughput** | 100% of queued signals processed by workers |
| **IBKR Max Submission Rate** | Capped cleanly by `IBKRExecutionScheduler` rate limit |

---

## 3. Automated Load Test Harness

The test script `backend/scripts/load_test_mft_burst.py` uses `httpx` async client to fire concurrent webhooks against the API:

```python
import asyncio
import time
import httpx

TARGET_URL = "http://127.0.0.1:8000/api/webhooks/tradingview"
BURST_COUNT = 300

async def send_signal(client: httpx.AsyncClient, index: int):
    payload = {
        "strategy_id": "model_blue",
        "trade_id": f"LOAD-TEST-{index:04d}",
        "signal_id": f"SIG-LOAD-{index:04d}",
        "action": "OPEN",
        "pair": "AAPL/MSFT",
        "side": "BUY",
        "ref_price_a": "150.00",
        "ref_price_b": "300.00"
    }
    start = time.monotonic()
    response = await client.post(TARGET_URL, json=payload)
    elapsed_ms = (time.monotonic() - start) * 1000.0
    return response.status_code, elapsed_ms, response.json()

async def main():
    async with httpx.AsyncClient(timeout=10.0) as client:
        start_time = time.monotonic()
        tasks = [send_signal(client, i) for i in range(BURST_COUNT)]
        results = await asyncio.gather(*tasks)
        total_time = time.monotonic() - start_time
        
        latencies = [r[1] for r in results]
        status_codes = [r[0] for r in results]
        
        print(f"Total Burst Duration: {total_time:.2f}s")
        print(f"Requests: {len(results)}, Successful: {status_codes.count(202)}")
        print(f"p50: {np.percentile(latencies, 50):.2f}ms")
        print(f"p95: {np.percentile(latencies, 95):.2f}ms")
        print(f"p99: {np.percentile(latencies, 99):.2f}ms")

if __name__ == "__main__":
    asyncio.run(main())
```

---
*Load Testing Specification.*
