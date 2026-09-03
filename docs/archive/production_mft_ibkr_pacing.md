> **STALE — do not follow.** Competing ceiling (40 vs 30/50) and a deleted scheduler class. Live pacing is `GatewayRateLimiter` in `backend/app/broker/ibkr/gateway_rate_limiter.py`. See [`backend-rms-oms.md`](../backend-rms-oms.md).

# Production MFT IBKR Execution Scheduler & Pacing Specification

This document details the centralized IBKR execution scheduling layer, token bucket request rate limiting, and connection health management.

---

## 1. Objectives of the Execution Scheduler

1. **Protect TWS Gateway Connection**: Prevent socket buffer overflow or TWS order drop errors caused by 300+ signals attempting simultaneous order submissions.
2. **Enforce Rate Limits**: Restrict broker API call rates below IBKR TWS thresholds (e.g. max 40 order messages/sec, max 50 contract details requests/sec).
3. **Prioritize Critical Operations**: Ensure cancellation and square-off compensation requests take precedence over new order submissions.

---

## 2. Architecture & Queue Structure

```
                          Worker Tasks (Order Submissions)
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │    IBKR Execution Scheduler Queue     │
                     │                                       │
                     │  Priority 1: Cancellations / Unwind   │
                     │  Priority 2: New Orders (OPEN/CLOSE)  │
                     │  Priority 3: Contract Details Lookup  │
                     └───────────────────┬───────────────────┘
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │   Leaky Bucket / Token Bucket Gate    │
                     │   - Max Rate: 40 ops/sec              │
                     │   - Max Concurrent Requests: 10       │
                     └───────────────────┬───────────────────┘
                                         │
                                         ▼
                             TWSClient.placeOrder()
```

---

## 3. Rate Limiter Implementation Design

```python
class IBKRExecutionScheduler:
    """Centralized rate-limiter and priority gate for IBKR TWS API requests."""

    def __init__(
        self,
        tws_client: TWSClient,
        max_rate_per_sec: float = 40.0,
        max_concurrent: int = 10,
    ) -> None:
        self._client = tws_client
        self._max_rate = max_rate_per_sec
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue = asyncio.PriorityQueue()
        self._tokens = max_rate_per_sec
        self._last_fill = time.monotonic()
        self._lock = asyncio.Lock()

    async def submit_order_paced(self, order: OMSOrder, contract: Any, ibkr_order: Any) -> int:
        """Paced submission of an order to TWS."""
        await self._acquire_token()
        async with self._semaphore:
            return self._client.placeOrder(order.ibkr_order_id, contract, ibkr_order)

    async def _acquire_token((self) -> None:
        """Token bucket algorithm guaranteeing max_rate_per_sec."""
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_fill
                self._tokens = min(self._max_rate, self._tokens + elapsed * self._max_rate)
                self._last_fill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep(1.0 / self._max_rate)
```

---

## 4. Operational Priority Order

1. **Priority 0 (Immediate)**: Order cancellation requests (`cancelOrder`) during unwind/compensation.
2. **Priority 1 (High)**: Compensation square-off order placements (`placeOrder` for unwind).
3. **Priority 2 (Normal)**: Strategy OPEN and CLOSE order placements.
4. **Priority 3 (Background)**: Contract details lookup (`reqContractDetails`) and market data re-subscriptions.

---
*IBKR Pacing Specification.*
