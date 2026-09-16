# 08-IBKR-BROKER.md — IBKR Integration, TWSClient & Rate Limiter

---

## 1. THE SINGLE TWSCLIENT ARCHITECTURE

All broker communication is channeled through a single instance of `TWSClient` ([`backend/app/broker/ibkr/tws_client.py`](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/broker/ibkr/tws_client.py#L22)):
- **No Multi-Gateway Pool**: The application connects to **one** TWS or IB Gateway socket (port 4001 default). Multi-account trading multiplexes by setting `ib_order.account` on each order.
- **Threading Model**: Inherits from both `EClient` and `EWrapper`. On startup, `client.connect_and_start()` launches a dedicated daemon thread running `self.run()`.
- **Thread Safety**: All state variables (`next_order_id`, `_listeners`, `_connected_event`) are synchronized via `threading.Lock`.

---

## 2. OUTBOUND PACING (`GatewayRateLimiter`)

Outbound API calls are paced by an in-process token bucket (`GatewayRateLimiter`) to comply with IBKR’s 50 msg/sec ceiling and avoid Error 100 disconnects:

### Priority Tiers
| Priority | Constant | Permitted Rate | Description |
|---|---|---|---|
| **P0** | `PRIORITY_EMERGENCY_FLATTEN` | Up to 30 msg/sec | May consume the emergency reserve slice (6 msg/sec) even when normal bucket is empty. |
| **P1** | `PRIORITY_ORDER_EXECUTION` | Up to 24 msg/sec | Standard order placement and cancellation. Requires normal token. |
| **P2** | `PRIORITY_CONTRACT_DETAILS` | Up to 24 msg/sec | Contract specification queries. |
| **P3** | `PRIORITY_MARKET_DATA` | Up to 24 msg/sec | Market data tick subscriptions (`reqMktData`). |
| **P4** | `PRIORITY_DIAGNOSTIC` | Up to 24 msg/sec | Health checks, account summary probes. |

### Error 100 Backoff
If IBKR emits error code 100 ("Max rate of messages exceeded"), the rate limiter pauses all outbound traffic for `error100_cooldown_sec` (2.0 seconds) to allow gateway recovery.

---

## 3. ORDER ID ALLOCATION RULES

- `client.allocate_next_order_id()` reserves IDs under `_order_id_lock`.
- **Strict Prohibition**: Never hardcode, guess, or default order ID to 1 if unconfirmed. If `next_order_id` is `None`, it strictly raises `RuntimeError`.

---

## 4. CONNECTION RECOVERY & AUTO-RESTORE

When the broker socket closes unexpectedly:
1. `connectionClosed()` triggers `_reconnect_thread`.
2. The background thread retries `eConnect()` with exponential backoff until restored.
3. Upon reconnect, `nextValidId()` resets the order ID sequence.
4. `LivePnlService` re-subscribes market data ticks for all open positions.
5. In-flight orders that were disconnected are investigated by `RecoveryManager` and `PositionReconciler`.
