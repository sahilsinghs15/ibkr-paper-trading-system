# Order lifecycle correctness and money math — review findings

**Scope:** order lifecycle state machine, partial fills, sign/direction, numeric
correctness, P&L and exposure. Concurrency, restart semantics and code hygiene are
explicitly out of scope and are only mentioned where a lifecycle defect *also* has a
restart face.

**Read at:** commit state of 2026-09-03. Paths are relative to `/home/tradingapp/app`,
so `backend/app/oms/ibkr_adapter.py:585` means
`/home/tradingapp/app/backend/app/oms/ibkr_adapter.py` line 585.

**Corrections to `docs/review/MAP.md` §Corrections that matter here:** the brief's
"pair-level P&L from our own fill ledger" is only half true, and the map already says so.
This review confirms it and adds a third authority: `positions.live_pnl` is written by
*two* different producers with different meanings (market-data ticks while open, realised
P&L on close — `backend/app/db/repositories/position_repository.py:224`).

**Production configuration assumed throughout:** `order_type="MARKET"`
(`backend/app/main.py:71`), `ibkr_port` defaults to 7497
(`backend/app/core/config.py:46`), and `execution_settings` defaults to
`enabled=True, max_retries=3` (`backend/app/db/models/execution_settings.py:16-18`).
Because `paper_retry_ports_allowed` (`backend/app/oms/retry_policy.py:10-12`) returns
True for paper ports, the auto-square-off retry path **is live** on the default
configuration. Several findings below are gated on that.

---

## 1. The state machine as implemented

### 1.1 Order states

Seven, at `backend/app/oms/models.py:15-21`: `PENDING`, `SUBMITTED`,
`PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, `ERROR`. Terminal set is
`_TERMINAL_STATUSES` at `backend/app/oms/ibkr_adapter.py:551-556`:
`FILLED`, `CANCELLED`, `REJECTED`, `ERROR`. The basket coordinator keeps its own copy of
the same tuple at `backend/app/oms/coordinator.py:42-47`.

### 1.2 Where transitions are written

There are **five** writers of `OMSOrder.status`, and only one of them respects the
terminal set:

| Writer | File:line | Terminal guard? |
|---|---|---|
| `_apply_mapped_status` (used by `orderStatus` and `openOrder`) | `ibkr_adapter.py:575-611` | yes, `:585` |
| `on_exec_details` direct assignment | `ibkr_adapter.py:828-831` | **no** |
| `on_error` | `ibkr_adapter.py:966-1000` | yes, `:967-968` |
| `on_connection_closed` | `ibkr_adapter.py:1020-1030` | yes, `:1022-1027` |
| Pre-broker rejects in `OMSService` / `submit_order` | `oms_service.py:240`, `:291`; `ibkr_adapter.py:146-165`, `:262`, `:387`, `:447` | n/a (order not yet live) |

Broker status strings map through `_map_ib_status` (`ibkr_adapter.py:558-573`).
Unmapped strings fall through to `PENDING` (`:573`) — `PendingCancel` is mapped to
`SUBMITTED` (`:563`), so there is no distinct "cancel requested" state.

### 1.3 Transitions that are unhandled or silently ignored

- **Any inbound transition on an order already terminal, via `orderStatus`/`openOrder`,
  is dropped with no log line at all** (`ibkr_adapter.py:585-586` is a bare `return`).
  That is the correct policy but it is invisible: there is no counter, no warning, and no
  event row, so a broker that contradicts our terminal decision leaves no trace.
- **The same transition via `execDetails` is not dropped** — see finding 1. This is the
  single largest inconsistency in the machine.
- **`PARTIALLY_FILLED` has no exit of its own.** Nothing times out an order; only the
  *basket* has `fill_timeout` (`coordinator.py:327`) and `cancel_timeout` (`:417`). An
  order that the broker stops talking about stays `PENDING`/`SUBMITTED`/
  `PARTIALLY_FILLED` in memory and in `orders.status` forever — see finding 12.
- **`CANCELLED` → `PARTIALLY_FILLED` is reachable** (finding 1), i.e. the machine is not
  acyclic and terminal is not terminal.
- **`FILLED` is reachable with `filled_quantity == 0`** (finding 2), so `FILLED` does not
  imply a filled quantity anywhere downstream.
- Basket states (`backend/app/oms/basket.py:11-21`) are `PENDING`, `EXECUTING`, `OPEN`,
  `CLOSED`, `UNWINDING`, `COMPENSATED`, `CRITICAL`, `RECOVERED`. `EXECUTING` with no fills
  has no exit at all — `recover_incomplete_baskets` explicitly leaves the row alone
  (`coordinator.py:507-515`).
- Position `risk_state` is only `OPEN`/`CLOSED`
  (`backend/app/db/repositories/position_repository.py:13-14`). `CLOSED` → `OPEN` is
  reachable by reusing a `trade_id` (`position_repository.py:175-191`) and does **not**
  reset `realised_pnl` / `commission` — see finding 18.

### 1.4 Remaining quantity — which authority

The brief asks whether remaining quantity is recomputed from the fill ledger or tracked
incrementally. The answer is *both, inconsistently*, across three writers:

| Field | Written at | Source |
|---|---|---|
| `filled_quantity` | `ibkr_adapter.py:589` | `max(existing, orderStatus.filled)` — monotone, broker-sourced |
| `filled_quantity` | `ibkr_adapter.py:815` | `max(existing, execution.cumQty)` — monotone, broker-sourced |
| `filled_quantity` | `ibkr_adapter.py:818-820` | `sum(e.quantity for e in order.executions.values()) + shares` — **ledger-sourced**, but only reached when `cumQty` is absent or zero |
| `remaining_quantity` | `ibkr_adapter.py:591` | `float(orderStatus.remaining)` — raw broker value, **not** monotone and not derived |
| `remaining_quantity` | `ibkr_adapter.py:816`, `:821` | `max(0, order.quantity - order.filled_quantity)` — derived |

So the ledger is the authority only on the fallback branch. The monotone `max()` on
`filled_quantity` is what actually protects against a stale or duplicated callback
reducing a fill, and it holds. `remaining_quantity` has no such protection, but it is
read in exactly one place — the partial-fill branch at `:593-604`, inside a function
that has already returned for terminal orders — so a stale `remaining` cannot change a
settled outcome. Every downstream consumer (`_basket_complete` at `coordinator.py:588`,
`_open_trade_from_fills` at `persistence.py:77`, `_intent_with_fills` at
`order_manager.py:1186`, `record_oms_order` at `order_repository.py:88`) reads
`filled_quantity`, never `remaining_quantity`. Findings 1 and 2 are both about
`filled_quantity`; `remaining_quantity` drift is inert.

### 1.5 Duplicated callbacks

Handled well for the *ledger*: `_seen_exec_ids` (`ibkr_adapter.py:810-811`),
`_commissioned_exec_ids` (`:916`), `_broker_acked` / `_fill_event_emitted` /
`_partial_qty_emitted` (`:697-728`), plus the `executions.exec_id` unique constraint
(`backend/app/db/models/execution.py:63`) and the `on_conflict_do_update` upsert
(`backend/app/db/repositories/execution_repository.py:110-117`). A duplicated
`execDetails` cannot double-count a fill quantity or a commission. The gap is that the
*status* mutation at `ibkr_adapter.py:828-831` runs regardless of `is_new_exec`.

---

## 2. Findings

| # | Severity | Location | Trigger | What breaks | Observable symptom | Confidence |
|---|---|---|---|---|---|---|
| 1 | **P0** | `backend/app/oms/ibkr_adapter.py:786`, mutation at `:814-831` (no guard, contrast `:585`) | `execDetails` for a genuine fill arrives *after* that order's terminal `orderStatus` (typically `Cancelled` following the basket's cancel at `coordinator.py:409`) | The fill is written to the `executions` ledger and `orders.fill_qty`, the order is regressed from `CANCELLED` to `PARTIALLY_FILLED`, and it is never compensated: `_compensate_filled` already read `filled_quantity` at `coordinator.py:884` and returned | `orders` row with `status='CANCELLED'` and `fill_qty > 0`, an `executions` row pointing at it, and **no** `is_compensation=true` sibling; `event_log` `PARTIAL_FILL` row timestamped after `BASKET_COMPENSATED`; log shows `IBKR execDetails callback: ... status=PARTIALLY_FILLED` after `Order ... status updated to CANCELLED`; IB account holds one naked leg | **certain** (defect) / trigger is assumption A1 |
| 2 | **P0** | `backend/app/oms/ibkr_adapter.py:606-607`, interacting with `:585` | A callback whose status string maps to `FILLED` arrives before any quantity does — `openOrder` carries no fill quantity at all (`:768` calls `_apply_mapped_status` with `qty_filled=None`) | `order.status` becomes `FILLED` while `filled_quantity` is still `0`. The order is now terminal, so the real `orderStatus`/`execDetails` quantities are discarded at `:585`. `_basket_complete` (`coordinator.py:586-591`) reads `filled_quantity == 0`, declares the leg unfilled, compensates nothing (`coordinator.py:884` skips it), and `_compensation_complete([])` returns True at `:594-595` → basket `COMPENSATED` | `orders` row `status='FILLED'`, `fill_qty=0`, `fill_price` null; `baskets` row `COMPENSATED` with zero compensation orders; reconciler logs `BROKER_ORPHAN` for both legs; position live at IB with no `positions` row | **likely** / assumption A2 |
| 3 | **P0** | `backend/app/services/kill_switch.py:483-499` | Kill-switch flatten where leg A's close fills and leg B's does not: the coordinator compensates by *re-buying* leg A (`coordinator.py:871-961`), producing a second FILLED order whose `internal_order_id` also contains `KILLSWITCH-` (`oms_service.py:295-311` on signal id `…:UNWIND:L0`) | The filter at `:483-486` does not exclude `is_compensation`, so `len(filled_close) == 2 >= req_legs` and `close_trade` marks the pair `CLOSED` — while the broker still holds the *entire* original position (A restored, B never closed). `exit_marks` is keyed by symbol (`:494`) so both rows collide on A and realised P&L is computed from one leg with the *compensation* price | `positions.risk_state='CLOSED'` with `closed_at` set, `kill_switch_operations.status='COMPLETE'`, `unresolved_count=0`, and IB still showing both legs; `orders` shows a FILLED `…:UNWIND:L0` row for the same trade_id; `realised_pnl` reflects only leg A | **certain** |
| 4 | **P0** | `backend/app/services/model_blue/persistence.py:64-70`, reached from `coordinator.py:360` → `order_manager.py:1320` | A 2-leg OPEN that completes via auto-square-off retry: `submitted` is extended with the retry order (`coordinator.py:360`), so `exec_res.orders` holds 3 non-compensation child orders | `_open_trade_from_fills` raises `POSITION_REQUIRES_FILLS … got 3 child orders`. `ModelBlueValidationError` subclasses `ValueError` (`model_blue/parser.py:15`) so it is swallowed as an account rejection at `order_manager.py:772-787`. The basket is `OPEN`, the execution claim was already sealed at `:1101`, and in-memory exposure/`processed_signals` were already written at `:1283-1300` — but **no `positions` row exists**. A later CLOSE hits `NO_OPEN_POSITION` at `model_blue/strategy.py:221-226`, because the trade book is the `positions` table (`db_trade_book.py:33-38`) | `baskets.state='OPEN'` and FILLED `orders` rows for a `trade_id` with **no** `positions` row; `signals.status='REJECTED'` with `POSITION_REQUIRES_FILLS` in `reject_reason`; log `AUTO-SQUARE-OFF retry: … action=SUBMIT` followed by `Account N rejected signal`; reconciler `BROKER_ORPHAN`; no `live_pnl`; every CLOSE for that trade_id rejected forever | **certain** / assumption A3 |
| 5 | **P1** | `backend/app/services/order_manager.py:1288-1293` (open) vs `:1305-1310` (close), with notional recomputed at `:1200-1207` | Any normal open-then-close cycle where the price moved | Exposure is *booked* at entry fill prices and *released* at exit fill prices, because both paths use `leg.effective_notional` of the `filled_intent` from `_intent_with_fills`. Price up → release exceeds booking, `max(Decimal(0), …)` clamps and silently eats other positions' exposure on the same symbol. Price down → a residue is stranded on that `(account, symbol)` key permanently | `MONEY_LIMIT_EXCEEDED: Symbol 'X' total exposure of … (existing … + new …)` in logs where `existing` does not match any open `positions` row; or `symbol_exposures` at 0 while positions are open, letting sizing exceed the per-symbol budget. Drift accumulates until restart re-seeds from `_add_row_exposure` (`:610-627`) | **certain** |
| 6 | **P1** | `backend/app/services/kill_switch.py:429-462`; `backend/app/services/position_close_service.py:200-236` | Any kill-switch flatten or single-pair close | Both write `positions` and `event_log` directly and never touch `RMSContext`. `open_positions`, `symbol_exposures`, `model_value_used` and `margin_commitments` keep counting the closed pair as open, and `LivePnlService.unwatch` is never called (contrast `order_manager.py:1111`) so tick subscriptions and `_legs` entries leak | `OPEN_POSITION_LIMIT_REACHED: … has N open position(s)` where the DB has fewer; `MONEY_LIMIT_EXCEEDED` on a symbol with no open row; `LivePnl` continues logging recomputes for a closed `trade_id`. Persists until restart | **certain** |
| 7 | **P1** | `backend/app/services/order_manager.py:1100`, `:1115` → `_record_unsettled_exposure` `:1210-1250` | A basket that reaches `COMPENSATED` (fully unwound — nothing left at the broker) | `COMPENSATED` is not in `(OPEN, CLOSED)` at `:1100`, so exposure is booked from `basket_res.orders` (the *submitted* fills) as if the risk were still on. For an OPEN that got fully unwound, exposure is added for a position that no longer exists. For a CLOSE that got unwound (compensation *restored* the position), exposure is **subtracted** for a position that is still open. The docstring at `:1213-1224` justifies this by "real risk sat at the broker", which is exactly what is not true in the `COMPENSATED` case | `UNSETTLED_EXPOSURE_BOOKED: trade_id=… action=OPEN` in the log immediately after `BASKET_COMPENSATED` for the same trade_id; `symbol_exposures` non-zero with no open positions | **certain** |
| 8 | **P1** | `backend/app/db/repositories/position_repository.py:220-223`; commission sourced at `backend/app/services/model_blue/persistence.py:216-218` | Every close | `commission` is only ever computed from the **close** basket's orders, so entry commissions never reach realised P&L. `row.commission = commission` also *assigns* rather than accumulates, and `persist_open` (`persistence.py:146-201`) writes no commission at all, so there is nowhere for the entry leg to live | `positions.commission` ≈ half of `SELECT sum(commission) FROM executions e JOIN orders o ON e.order_id=o.id WHERE o.trade_id=…`; `positions.realised_pnl` consistently optimistic by roughly one round-trip's entry commission on every closed pair | **certain** |
| 9 | **P1** | `backend/app/services/model_blue/persistence.py:28-38` | A close basket that produces two orders on the same symbol — i.e. the retry path (original partially filled + retry order for the remainder) | `marks[order.symbol] = raw` (`:37`) overwrites, so the exit mark is whichever order iterates last rather than the quantity-weighted average across both fills. `close_trade` then multiplies the pair's **full** `signed_qty` by that single price | `positions.realised_pnl` that cannot be reproduced from the `executions` rows for that trade_id — exactly the invariant `test_persisted_executions_reproduce_realized_pnl` (`backend/tests/test_execution_audit_persistence.py:405`) asserts, but that test only covers the single-order-per-leg case | **certain** / assumption A3 |
| 10 | **P1** | `backend/app/db/repositories/position_repository.py:205-218`; worst caller `backend/app/services/kill_switch.py:490-499` | `exit_marks` missing an entry for one leg (symbol collision as in finding 3, or `fill_price IS NULL` filtered out at `kill_switch.py:493`) | Each leg is guarded by `if row.leg_x_symbol in exit_marks`, so a missing leg silently contributes `0` to realised P&L. The row is still marked `CLOSED` and `realised_pnl` still written. There is no "incomplete marks" error path | `positions.realised_pnl` equal to exactly one leg's P&L on a two-leg pair; `POSITION_CLOSE` event with `source: 'KILL_SWITCH'` and a realised figure that is roughly half the expected magnitude | **certain** |
| 11 | **P1** | `backend/app/services/model_blue/persistence.py:44-57` | `commissionReport` has not arrived for any exec of the close orders when `after_submit` runs | The `if execs:` branch `continue`s at `:51`, so the `order.commission` fallback is unreachable whenever executions exist, and `return total if found and total > 0 else None` (`:57`) returns `None`. `close_trade` then skips the commission block entirely (`position_repository.py:220`) | `positions.commission = 0` on a closed pair whose `executions` rows *later* get non-null `commission` (the adapter still attaches them at `ibkr_adapter.py:931-937`, but nothing re-derives `positions.commission`); realised P&L never corrected | **likely** |
| 12 | **P1** | No reaper exists: `orders.status` is only written by `record_oms_order` (`backend/app/db/repositories/order_repository.py:65-172`); the only sweeps are `signal_jobs` (`signal_repository.py:486`), `baskets` (`coordinator.py:477-555`) and `execution_claims` (`recovery.py:79`) | Broker goes silent on a working order without closing the socket | An order sits in `PENDING`/`SUBMITTED`/`PARTIALLY_FILLED` indefinitely. `coordinator.recover_incomplete_baskets` explicitly leaves a no-fill incomplete basket in `EXECUTING` (`:507-515`), and `fetch_in_flight_accounts` (`position_reconciler.py:455-479`) treats `EXECUTING` as in-flight — so that account's reconcile diffs are stamped `in_flight=true` forever, which is precisely the flag an operator would use to dismiss a mismatch | `SELECT status, count(*) FROM orders GROUP BY 1` showing non-terminal rows older than the session; `baskets.state='EXECUTING'` rows with `updated_at` from previous days; every `position_reconcile_runs.mismatches` entry for that account carrying `"in_flight": true` | **certain** |
| 13 | **P1** | `backend/app/services/recovery.py:66-73` requests the snapshot; `backend/app/oms/ibkr_adapter.py:453-461` (`adopt_order`) has no callers anywhere in `backend/` | Startup recovery after any restart with live orders | `fetch_broker_order_snapshot` fires `reqOpenOrders` and `reqExecutions(9003, …)` (`ibkr_adapter.py:468-477`), but `_orders_by_tws_id` is empty in the fresh process and nothing repopulates it, so every replayed `openOrder`/`execDetails` returns immediately at `:756-762` / `:796-798`. The comment at `recovery.py:67-69` says the call "warms adapter state" — it cannot | Log line `Requested open orders / executions snapshot from broker` followed by `Ignoring openOrder for unknown tws_id=…` at debug level and **no** new `executions` rows, even when the broker filled orders during the outage | **certain** |
| 14 | **P2** | `backend/app/oms/ibkr_adapter.py:1017-1030` | TWS socket drop with orders working | Every non-terminal order is force-set to `ERROR` on our own authority, with no attempt to ask the broker what actually happened. The coordinator then treats those as unfilled and drives compensation off a guess | `Connection closed unexpectedly` as `orders.status='ERROR'` for orders that IB later shows as filled; `BASKET_CRITICAL` clusters at the same timestamp as `connectionClosed` | **certain** (behaviour) / whether it is the intended fail-safe is assumption A4 |
| 15 | **P2** | `backend/app/oms/ibkr_adapter.py:892` via `_usable_price` `:54-63`; consumed at `position_repository.py:220` | IB reports a negative commission (exchange/liquidity rebate) | `_usable_price` only accepts `0 < value < 1e12`, so a rebate becomes the `fallback` of `Decimal(0)`; even if it survived, `if commission is not None and commission > 0` would skip it | `executions.commission = 0` on rebated fills; `positions.commission` understated | **speculative** / assumption A5 |
| 16 | **P2** | `backend/app/instruments/resolver.py:398-418` | Any leg on an instrument with a `size_increment` | `quantity` is replaced with the rounded value at `:411` but `notional` is carried over unchanged from the sizer (`model_blue/sizer.py:268`), and `effective_notional` (`rms/models.py:70-74`) returns the stored `notional` in preference to recomputing. RMS therefore evaluates the pre-rounding notional. The direction is conservative for the gate, but `_commit_margin` (`order_manager.py:391-406`) and the `INSTRUMENT_RESOLVED` audit rows record a notional that does not match the submitted quantity | `event_log` `INSTRUMENT_RESOLVED` detail where `quantity` × price ≠ the `margin_impact` recorded on the matching `orders` row | **certain** |
| 17 | **P2** | `backend/app/services/position_reconciler.py:343-452` | Any broker-vs-ledger divergence | The reconciler classifies `LEDGER_GHOST` / `QTY_DRIFT` / `BROKER_ORPHAN` and writes them to `position_reconcile_runs` and `event_log`, but performs no repair and raises no alert path — it is the only component that could catch findings 1-4, and it only logs | `position_reconcile_runs.ghost_count`/`drift_count` non-zero on every 30 s sweep for days, with `positions` unchanged | **certain** (deliberate per the module docstring `:1` — flagged for operator awareness, not as a defect) |
| 18 | **P2** | `backend/app/db/repositories/position_repository.py:175-191` | Re-opening a `trade_id` that was previously closed on the same account | The existing row is mutated back to `risk_state='OPEN'` with `closed_at=None`, but `realised_pnl`, `commission` and `live_pnl` are left holding the *previous* cycle's values. The next `close_trade` overwrites them, so the earlier cycle's realised P&L is destroyed. `positions` is also the only closed-trade store (`backend/demo_streaming/api.py:177`) | A freshly opened pair whose `/demo/positions` row already shows a non-zero `realised_pnl`; closed-trade history for that `trade_id` silently replaced | **speculative** / assumption A6 |
| 19 | **P3** | `backend/app/services/order_manager.py:1325-1338` | Legacy single-name path | `target_qty = int(intent.legs[0].quantity)` truncates, and the resulting *share count* is added to `open_positions`, the same dict `OpenPositionLimitCheck` compares against `max_open_positions` (`rms/checks/position_limit.py:44-45`). 100 shares would exhaust a 100-position budget | n/a in production — the path is unreachable whenever `_account_router` is set, which `session_factory` guarantees (`order_manager.py:136-138`, raise at `:699-702`) | **certain** (dead code) |
| 20 | **P3** | `backend/app/oms/ibkr_adapter.py:807-809` | `execDetails` with an empty `execId` | The synthetic id `noid:{iid}:{shares}:{price}:{cum_qty}` collides for two identical fills when `cumQty` is also absent, so the second fill is dropped from the ledger and from `filled_quantity` (`:817-820` adds nothing when `is_new_exec` is False) | An `executions` row count lower than the broker's fill count for one order, with `orders.fill_qty` short by exactly one clip | **speculative** / assumption A7 |
| 21 | **P3** | `backend/app/services/pnl.py:739-743` | A trade with more than two legs | `_recompute` sums `symbols[0]` and, if present, `symbols[1]`, then stops. Legs 3+ contribute nothing to `live_pnl` | n/a today — `positions` is a hard two-leg schema (`position_repository.py:143-144`), so this is a latent trap rather than a live bug | **certain** (latent) |
| 22 | **P1** | `backend/app/oms/coordinator.py:865` mints the id; `:1040` and `:1058-1059` strip only `:UNWIND:` and `:CLOSE` | A leg that completes via auto-square-off retry | `_retry_intent` sets `signal_id=f"{original.signal_id}:RETRY:L{index}:{attempt}"` (`:865`), and **no application code strips `:RETRY:`** — a repo-wide search for the literal finds it only at that line plus `parent_signal_id` filters inside `backend/tests/test_basket_retry.py`. `_persist_broker_snapshot` persists on the broker-callback path with `order.intent` (`:1184`), i.e. the *retry* intent, so `record_oms_order` inserts `trade_id='{orig}:RETRY:L0:1'`. The later `_persist_child(order, intent)` from the coordinator loop (`:362`) passes the original intent but cannot correct it: the `on_conflict_do_update` `set_` at `order_repository.py:140-156` does not include `trade_id`, which is insert-only. `_ensure_signal_pk` (`:1058`) likewise mints a **second `signals` row** for the retry id. `OrderRepository.list_by_trade_id` (`order_repository.py:51-55`) — the query kill-switch reconciliation uses at `kill_switch.py:482` — therefore cannot see the retry fill | `orders` rows whose `trade_id` looks like `MBG-EWA-EWC-0912:RETRY:L0:1` alongside siblings with the bare `MBG-EWA-EWC-0912`; an extra `signals` row with that `signal_id` and `raw_payload.source='oms_signal_fk'`; `SELECT * FROM orders WHERE trade_id='<trade>'` returning fewer rows than `WHERE basket_id=<id>`; kill-switch reconciliation under-counting closed legs for a retried trade | **certain** that `:RETRY:` is never stripped / **likely** that the callback path wins the insert race — assumption A3 |
| 23 | **P2** | `backend/app/services/order_manager.py:1186-1197` | A retried leg: two non-compensation orders on one `leg_index` at different fill prices | `qty` sums `filled_quantity` across **all** matching orders (`:1186`), but the price loop `break`s on the **first** order that has a price (`:1188-1197`). The leg is rebuilt as `quantity=total, price=first_clip_price, notional=total × first_clip_price` (`:1200-1207`), which is not the quantity-weighted average of the fills. That notional is what `_update_runtime_state` books into `symbol_exposures` (`:1290-1293`) and `model_value_used` via `intent_market_value` (`:1298`). `positions` itself is safe — `_open_trade_from_fills` recomputes from the orders (`persistence.py:82-88`) — so the corruption is confined to the in-memory risk budget | `MONEY_LIMIT_EXCEEDED` / `MODEL_MARKET_VALUE` rejection messages whose `existing` figure cannot be reproduced from `SELECT sum(quantity*price) FROM executions` for that account+symbol; the gap equals `retry_qty × (first_price − retry_price)`. Self-heals on restart via `_add_row_exposure` (`:610-627`) | **certain** / assumption A3 |
| 24 | **P2** | `backend/app/db/repositories/position_repository.py:207-218` | A close whose filled quantity is smaller than the open quantity recorded on the row | Realised P&L is `row.leg_a_signed_qty * (exit − entry)` using the quantity written at **open** (`:159`, `:162`). Nothing in `close_trade` — or in any caller — compares that against the close basket's filled quantity, and there is no partial-close representation in the schema: `risk_state` is only `OPEN`/`CLOSED` (`:13-14`). The reachable route is `apply_size_increment` (`resolver.py:400-407`), which `attach_resolved` applies to close legs too; `position_close_service.py:170` sizes its legs from `abs(pos.leg_a_signed_qty)` and then runs them through `_resolve_instruments`, so a `size_increment > 1` on the instruments row rounds the close **down** while the row is still closed at full size | `positions.risk_state='CLOSED'` with realised P&L on the full quantity, while `broker_positions` still carries a line for that symbol and every subsequent `position_reconcile_runs` row reports `BROKER_ORPHAN` for it; `orders.quantity` on the close leg strictly less than `abs(positions.leg_a_signed_qty)` | **certain** that the close quantity is never cross-checked / **speculative** that a down-rounding close occurs — assumption A9 |
| 25 | **P3** | `backend/app/oms/models.py:109-115` and `backend/app/db/repositories/execution_repository.py:30-36` | Two legs of a pair (or two fills of a leg) commissioned in different currencies | Both commission totals iterate rows and `total += commission` with no reference to `commission_currency`, which *is* captured (`ibkr_adapter.py:932`) and stored (`db/models/execution.py:45`). `close_trade` then subtracts that mixed-currency sum from a P&L computed in the legs' quote currency (`position_repository.py:220-223`). There is no FX conversion anywhere in `backend/app/` | `executions` rows for one `trade_id` with more than one distinct `commission_currency`, and a `positions.commission` that is a bare arithmetic sum of them | **speculative** / assumption A8 |

### Assumptions named

- **A1** — IBKR never delivers an `execDetails` for an order after that order's terminal
  `orderStatus`. If this holds, finding 1 is unreachable in the same process. Note that
  the code has no guard either way, and `reqExecutions` replay makes the ordering
  explicitly not guaranteed (it is only saved by finding 13, which drops the replay).
- **A2** — IBKR never delivers an `openOrder` (or an `orderStatus`) whose status maps to
  `FILLED` before the callback carrying the filled quantity. Finding 2 depends entirely on
  this. `openOrder` structurally cannot carry a fill quantity, so any `openOrder` with
  `orderState.status == "Filled"` triggers it.
- **A3** — the auto-square-off retry path can execute. True on the default config (paper
  port 7497 + `execution_settings.enabled=True`); false on live ports 7496/4001, where
  `_retries_enabled` returns False at `coordinator.py:631-635`. Findings 4 and 9 are live
  on paper and dormant on a live port.
- **A4** — marking working orders `ERROR` on socket drop is the intended fail-safe rather
  than a state-machine bug.
- **A5** — IB can report negative commissions on this account type.
- **A6** — `trade_id` values from TradingView can repeat for one account. RMS check 2
  (`rms/checks/duplicate.py:30`) plus `processed_signals` hydration
  (`order_manager.py:198-218`) should prevent it while the `signals` row survives, which
  is why this is P2/speculative rather than P1.
- **A7** — `execDetails.execId` can be empty. The fallback at `:807-809` exists, so
  someone thought it could.
- **A8** — every account trades a single currency, so summing `executions.commission`
  without reading `commission_currency` is safe. True today for US STK/CFD on USD
  accounts; finding 25 is dormant until a non-USD leg appears.
- **A9** — some `instruments` row carries `size_increment > 1`. Finding 24's rounding
  route needs this; the field is nullable and the seeders may never populate it above 1,
  in which case only the missing cross-check remains, not a live divergence.

### Negative results (checked, clean)

- **No float equality on money anywhere.** The only `== 0` comparisons on numerics are
  `oms/models.py:152` (quantity initialisation) and `kill_switch.py:510` (an integer
  count). P&L comparison at `pnl.py:772` is `Decimal == Decimal` used as a
  write-deduplication key, which is exact and correct.
- **No integer division or `%` on money or quantity.** The only `//` in `backend/app/`
  outside URLs is uptime formatting (`watchdog/status.py:346-347`).
- **Prices and quantities are `Decimal` at every persistence boundary** —
  `execution_repository.py:14-27` and `order_repository.py:87-96` convert before writing,
  and `Numeric(18,8)` is used for all price and P&L columns
  (`db/models/execution.py:42-46`, `db/models/position.py:28-39`). `float` is used only
  for `OMSOrder.quantity`/`filled_quantity`/`remaining_quantity`
  (`oms/models.py:126-130`) and `OrderLeg.quantity` (`rms/models.py:56`), which is a
  deliberate match for `ibapi`'s `Order.totalQuantity` float, and every read of it
  converts via `Decimal(str(...))`. Quantities are still `Decimal` where it decides money:
  the sizer works in `Decimal` throughout (`sizer.py:257-268`) and only narrows at
  `strategy.py:270`, and `effective_notional` re-widens via `Decimal(str(self.quantity))`
  (`rms/models.py:74`). The one place a float *accumulates* is
  `sum(float(o.filled_quantity) for o in matching)` at `order_manager.py:1186` — with
  whole-share STK quantities this is exact, so it is finding 23's price selection that
  corrupts the notional there, not the float sum.
- **Lot-size rounding cannot silently round to zero.** `apply_size_increment`
  (`resolver.py:47-55`) is checked at `resolver.py:402-407`, the sizer rejects sub-share
  STK at `sizer.py:260-263`, and `order_manager.py:1031-1035` and `:1495-1500` both
  re-assert positive quantity after rounding. Three independent guards.
- **A duplicated fill cannot be double-counted in the ledger** —
  `_seen_exec_ids` (`ibkr_adapter.py:810`), `uq_executions_exec_id`
  (`db/models/execution.py:63`) and the commission-once path (`:916`, `:935`) all hold.
- **Sign handling is consistent.** `_signed_qty` (`position_repository.py:17-19`),
  `_leg_from_signed` (`:22-37`), `unrealized_leg` (`pnl.py:40-42`) and
  `realized_pnl_from_marks` (`execution_repository.py:39-46`) all use the same
  `signed_qty * (exit - entry)` convention, and the three close-side reversals
  (`model_blue/strategy.py:230-232`, `kill_switch.py:362`/`:378`,
  `position_close_service.py:97`/`:112`) all derive the side from the stored signed
  quantity, not from the original open's side.
- **Closes are derived from the ledger, not from the original signal.** The trade book is
  the `positions` table (`db_trade_book.py:33-38`), and `positions` is itself rebuilt from
  actual fills (`persistence.py:60-118`). This matches the stated design intent.

### Questions rather than defects

1. **Exposure clamping.** `max(Decimal(0), …)` at `order_manager.py:1308` and `:1316`
   turns an accounting error into a silent floor. Is clamping preferred over raising, on
   the grounds that a negative exposure figure would reject all trading on that symbol?
   Finding 5 is a real asymmetry either way, but the clamp is what makes it invisible.
2. **CLOSE legs carry the entry price.** `model_blue/strategy.py:238` sets the close leg's
   `price` to the *entry* mark, which becomes `OMSOrder.limit_price` via
   `oms_service.py:313-321` and is persisted to `orders.limit_price`. Harmless while
   `order_type="MARKET"` (`ibkr_adapter.py:232-233` ignores it), but it means the ledger's
   `limit_price` on a close is an entry price. Is that intended as a reference mark?
3. **`positions.live_pnl` is overwritten with realised P&L on close**
   (`position_repository.py:224`). Deliberate, so the dashboard shows a final figure in the
   same column?
4. **Reversal through zero.** Model Blue cannot flip a position in one order —
   `_build_open_intent` rejects an already-open `trade_id`
   (`model_blue/strategy.py:158-160`). Is a reversal meant to be an explicit CLOSE
   followed by an OPEN with a new `trade_id`, and should a single flipping order be
   rejected rather than silently unsupported?
5. **The `:RETRY:` signal id.** Finding 22 assumes a retry clip belongs to the original
   trade for ledger purposes. The `:UNWIND:` suffix is deliberately stripped back to the
   parent trade (`coordinator.py:1040`, `:1058`) while `:RETRY:` is not — is the retry
   meant to be a *distinct* trade in `orders`/`signals` for audit separation, with
   `basket_id` as the only intended join? If so, finding 22 is a documentation gap rather
   than a defect, but `kill_switch.py:482` still reads by `trade_id` and would need to
   change.
6. **`contract_month` on the flatten paths.** `kill_switch.py:370` uses `"202612"`,
   `position_close_service.py:105` uses `""`, and the signal path uses `"2026-09"`
   (`order_manager.py:91`). RMS check 4 is bypassed on both flatten paths (forged
   `RMSResult`), so nothing reads them — is the field simply vestigial for equities?

---

## 3. Reproducing tests

Written to `backend/tests/test_lifecycle_money_bugs.py`. Each test is named for the
finding it reproduces and **fails on the current code**.

| Finding | Test | Reproduces |
|---|---|---|
| 1 | `test_exec_details_after_cancel_does_not_regress_terminal_order` | Fill delivered after `Cancelled`: asserts the order stays `CANCELLED`. Currently regresses to `PARTIALLY_FILLED` with `filled_quantity=50`, i.e. an uncompensated fill |
| 2 | `test_filled_status_without_quantity_does_not_block_real_fill` | `openOrder(status="Filled")` then `orderStatus(filled=100)`: asserts `filled_quantity == 100`. Currently `0`, permanently |
| 3 | `test_kill_switch_does_not_count_compensation_order_as_close_fill` | Leg-A close FILLED + leg-A compensation FILLED + leg-B unfilled: asserts the position stays `OPEN` and the operation is `UNRESOLVED`. Currently `CLOSED` / `COMPLETE` |

**Verified by running them** (`.venv/bin/python -m pytest tests/test_lifecycle_money_bugs.py -q`
against the real `ibkr_trading_test` Postgres, 2026-09-03): `3 failed`, each on its own
assertion rather than on a fixture or import error, which is what makes them
reproductions rather than broken tests.

- Finding 1 — `AssertionError: terminal CANCELLED order regressed to PARTIALLY_FILLED;
  50.0 shares are filled at the broker with no compensation order and no non-terminal
  owner`.
- Finding 2 — fails on `filled_quantity == 100.0`, confirming the quantity is discarded
  permanently once `openOrder` has set `FILLED`.
- Finding 3 — `AssertionError: position marked CLOSED while the broker still holds both
  legs … (closed_at=2026-09-02 22:08:27+00:00, realised_pnl=520.00000000)`. The
  non-zero `realised_pnl` is the second half of the finding: 520 is computed from the
  **compensation** order's price, so the wrong number is written as well as the wrong
  state.

Findings 4, 5, 8, 22 and 23 also reproduce in a handful of lines each
(`_open_trade_from_fills` with three child orders; `_update_runtime_state`
OPEN-then-CLOSE at different fill prices; `close_trade` commission versus the
`executions` sum; `_persist_child` with a `:RETRY:` intent; `_intent_with_fills` over two
clips at different prices) but are not written here, per the brief's request for the top
three.

Worth recording why findings 4, 22 and 23 survived an otherwise well-covered feature:
`backend/tests/test_basket_retry.py` exercises the retry path thoroughly, but a search of
that file for `persist`, `positions`, `PositionRepository` or `after_submit` returns
nothing — it asserts on coordinator state and `oms.get_all_orders()` only. The retry path
is therefore fully tested up to the basket boundary and completely untested past it,
which is exactly where all three findings live.

None of the four new findings above needed a downgrade for want of a test —
22 and 23 are certain from the code path, and 24 and 25 are already labelled
speculative on their triggers, not on the code.
