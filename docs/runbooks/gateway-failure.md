# Gateway / TWS socket failure

**Verified from:** `backend/app/broker/ibkr/tws_client.py`, `backend/app/oms/ibkr_adapter.py`, `backend/app/oms/coordinator.py`, `backend/app/services/pnl.py`, `backend/app/services/recovery.py`, MAP §3.3.

This is the **one-socket** failure path. There is no second Gateway and no clientId rotation.

## What the process does on drop

IBKR `connectionClosed` lands on the TWS reader thread (`TWSClient`) and fans out to listeners.

1. `IBKRExecutionAdapter.on_connection_closed` marks every non-terminal in-memory OMS order `ERROR` with `"Connection closed unexpectedly"`.
2. Fill waiters are **parked** — the fill future is not resolved as terminal. The coordinator must not compensate (`BASKET_COMPENSATED`) and must not treat `_compensation_complete([])` as success. Basket stays `EXECUTING`.
3. Disconnect-`ERROR` is **not** sticky in `orders.status` (overwritable on reconnect). Real TWS rejects still persist as terminal.
4. `LivePnlService` stops receiving ticks until reconnect; `on_connection_restored` resubscribes active contracts.

Do **not** “fix” this by stopping the in-memory `ERROR` mark. Park the aftermath; keep the mark.

## What reconnect does

`TWSClient` reconnects unless the drop was `_intentional_disconnect` (clean shutdown).

On success:

1. Allocate `next_order_id` under one lock. Never default `None → 1`.
2. Adapter unparks disconnect-`ERROR` → `SUBMITTED`.
3. `fetch_broker_order_snapshot` (`reqOpenOrders` / executions) books late fills.
4. Recovery **adopts** non-terminal `orders` rows into adapter maps **before** that snapshot (so `permId` / tws id land in `_orders_by_tws_id`).
5. Live P&L `_resubscribe_all_active`.

Until the socket is back, `submit_order` still raises `ConnectionError` if `not is_connected()`.

## Operator checks

| Check | What you want |
|-------|----------------|
| `ss -lntp \| grep 4001` | Gateway API listening |
| `ib_gateway.log` contains `Login has completed` | IB session actually up |
| trading log `IBKRExecutionAdapter detected connection closed` | Drop seen |
| trading log `connection restored; adopting live orders` | Reconnect + adopt |
| `baskets.state='EXECUTING'` with disconnect-ERROR children | Parked, not compensated |
| **No** `BASKET_COMPENSATED` on the same `trade_id` at drop time | M7 invariant |

systemd owns restarts (`trading-backend`, `ibgateway`). Watchdog observes and notifies; it does not `systemctl`. `process-manager.service` must stay disabled.

## What this is not

- Failover to a second host or a second `client_id`.
- Auto-flatten from the reconciler (sensor only until an operator / kill-switch / leftover script acts).
- A reason to put `execution_claims` on kill-switch / pair-close / broker flatten.
