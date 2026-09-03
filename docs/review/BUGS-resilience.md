# Resilience review — crash, restart, disconnect

**Scope:** crash / restart / disconnect behaviour only. Numeric correctness and code hygiene
are out of scope and were not assessed.

**Read at:** commit state of 2026-09-03. Paths are relative to `/home/tradingapp/app`, so
`backend/app/main.py:105` means `/home/tradingapp/app/backend/app/main.py` line 105.

**Method:** the signal path from `docs/review/MAP.md` §4 was walked hop by hop, asking at each
hop what is left behind if the process dies immediately after it, what recovery does, and
whether recovery converges or double-acts. The crash-point table at the end is the summary of
that walk; the findings below are the defects it surfaced plus the targeted hunts.

**One structural fact drives most of the P0s.** Every recovery decision in this system —
`ExecutionClaimRepository.reconcile_stale_claims`, `RecoveryManager.run_startup_recovery`,
`OrderManager._resolve_failed_claim`, the worker's own failure handler — asks the same
question: *did this intent already emit orders?* All four answer it with
`count_orders_emitted`, which counts rows in the `orders` table:

```151:167:backend/app/db/repositories/execution_claim_repository.py
    async def count_orders_emitted(self, strategy_id: str, signal_id: str) -> int:
        """Orders already written for this (strategy_id, signal_id).
        ...
        stmt = (
            select(func.count())
            .select_from(OrderModel)
```

That row is written *after* `placeOrder` returns. The gap between the two is the window in
which the system's entire recovery logic gives the wrong answer.

---

## Findings

| # | Severity | Location | Trigger | State left behind | Recovery behaviour | Observable symptom | Confidence |
|---|---|---|---|---|---|---|---|
| 1 | **P0** | `backend/app/oms/coordinator.py:266-275`, `backend/app/oms/ibkr_adapter.py:438`, `backend/app/db/repositories/execution_claim_repository.py:169-204` | Process dies after `placeOrder` and before the `orders` row commits | Live order at IB; `execution_claims` row `CLAIMED`; no `orders` row; job `PROCESSING` | Startup calls `reconcile_stale_claims(stale_after_sec=0.0)`, sees zero orders emitted, sets claim `ABANDONED`, requeues the job to `QUEUED` | Two IB order IDs for one `trade_id`; `Released stale execution claim ...` then `Recovery requeued job_id=...`; doubled position on the IB account | certain |
| 2 | **P0** | `backend/app/services/position_close_service.py:139,180`; `backend/app/services/kill_switch.py:396,418`; `backend/app/services/broker_flatten_service.py:150,183` | Operator retries a square-off / pair close / reconcile flatten after a restart, or two operators click at once across a restart | Two baskets, two sets of close orders | None — these paths never call `_acquire_execution_claim`, and their `signal_id` embeds a fresh `uuid4()` per attempt so no unique index can catch them | Duplicate `KILLSWITCH-…` / `CLOSEPAIR-…` / `RECON-FLAT-…` rows in `baskets` and `orders` for one `trade_id`; position flips from flat to net short/long at IB | certain |
| 3 | **P1** | `backend/app/db/repositories/signal_repository.py:380-386` vs `:513-527`; `backend/app/db/models/signal.py:57` | Event loop stalls > 30 s (lease duration) while a job is `PROCESSING` | Job re-claimed by a second worker while the first is still inside `_execute_job` | The claim query treats expired-lease `PROCESSING` as claimable, so a worker (0.5 s poll) beats the reclaimer's quarantine (15 s sweep) | `Worker X LOST its lease on job Y -- another worker may now own it`; the same `signal_id` appearing twice in `attempt_count` | certain |
| 4 | **P1** | `backend/app/main.py:85` and `backend/app/services/recovery.py:148-149`; accumulation at `backend/app/services/order_manager.py:211-219` | Any restart where at least one job or basket is non-terminal | `open_positions`, `symbol_exposures`, `model_value_used` all doubled in RMS memory | Nothing. Only `model_value_used` is later rebuilt, by `_reseed_model_value_used` on the 30 s reconcile sweep | `Hydrated runtime from DB` logged twice; RMS check 7 / check 8 rejecting valid signals with an inflated open count; `RMS_REJECT` events for `OPEN_POSITION_LIMIT` / `MONEY_PER_STOCK` | certain |
| 5 | **P1** | `backend/app/services/order_manager.py:224-229`, swallowed at `backend/app/main.py:84-87` | Anything in `hydrate_critical_from_db` or `recover_incomplete_baskets` raises during startup | Kill-switch blocked-account cache empty | None — `hydrate_kill_switch_cache` is the only rehydrator and it sits *after* the basket work in the same `try` | `Failed to hydrate Model Blue/RMS runtime state from PostgreSQL` with **no** following `KILL SWITCH REARMED FROM DB` line; new OPEN orders on an account whose `kill_switch_operations` row is still armed | certain (ordering) / likely (that it fires) |
| 6 | **P1** | `backend/app/services/recovery.py:66-73`; `backend/app/oms/ibkr_adapter.py:644-646, 756-762, 796-798, 454` | Any restart with orders working at IB | Orders working at IB that the process has no record of | `fetch_broker_order_snapshot()` fires `reqOpenOrders` / `reqExecutions`, but every callback looks up `self._orders_by_tws_id`, which is empty after a restart, and returns. `adopt_order` exists but is called from no application code | `Requested open orders / executions snapshot from broker` followed by `Ignoring openOrder for unknown tws_id=…` at debug; the order fills at IB and never appears in `orders`; the 30 s reconciler logs it as `BROKER_ORPHAN` and takes no action | certain |
| 7 | **P1** | `backend/app/services/recovery.py:49-53` and `:148-149`; `backend/app/oms/coordinator.py:482,488-496` | Crash mid-basket, then any exception inside `run_startup_recovery` before line 148 | Basket row stuck `EXECUTING`; no `CRITICAL` latch; OPENs unblocked | The first `recover_incomplete_baskets` pass runs at `main.py:85`, *before* TWS connects at `main.py:105`, so it always logs `BASKET_RECOVER_DEFER` and returns. The only pass that can escalate is the second one, at the tail of `run_startup_recovery`. `pending_baskets` itself is read into a list and never acted on | `BASKET_RECOVER_DEFER … (TWS disconnected)` at startup with no later `BASKET_RECOVER_CRITICAL`; `baskets.state='EXECUTING'` older than the current process; naked leg at IB with new OPENs still accepted | certain (deferral) / likely (escalation skipped) |
| 8 | **P0** | `backend/app/oms/ibkr_adapter.py:1017-1030`; sticky persist at `backend/app/db/repositories/order_repository.py:13,105-115`; latch clear at `backend/app/services/critical_recovery.py:298-300,231-240` | IB Gateway drops the socket while orders are working | Every non-terminal order forced to `ERROR` locally, while the orders are still live at IB | The `ERROR` status is persisted and is in `_TERMINAL_ORDER_STATUSES`, so a later real fill cannot overwrite it. `_collect_leftover_legs` then reads `fill_qty=0`, concludes there is nothing to flatten, and clears the CRITICAL latch | `Connection closed unexpectedly` on every open order; `orders.status='ERROR'` with `fill_qty=0`; `BASKET_CRITICAL_CLEARED … No filled non-compensation legs`; IB account shows a filled position the ledger says never happened | certain (marking + sticky persist) / likely (latch clear) |
| 9 | **P1** | `backend/app/broker/ibkr/tws_client.py:151-156`; `backend/app/oms/ibkr_adapter.py:203-209`; log claim at `backend/app/main.py:112` | Socket drops; or a submit races a drop between the connection check and the ID reservation | `next_order_id = None`, `managed_accounts` empty, no reconnect attempt anywhere | None. There is no reconnect loop. If a submit is in the window between `is_connected()` at `:386` and `_get_next_tws_order_id()` at `:403`, the reservation falls into `if current_id is None: current_id = 1` and reuses order ID 1 | `Initial TWS connection attempt unconfirmed; execution adapter will auto-reconnect on active traffic` (there is no such code); afterwards every submit raises `Cannot submit order: TWS is not connected`; or an IB duplicate-order-id rejection on ID 1 | certain |
| 10 | **P2** | `backend/app/oms/ibkr_adapter.py:250-264`, called at `:512` | Rate limiter is saturated when a cancel is attempted | A *working* order is set to `status = ERROR` locally because we could not get a token to cancel it | The order is now locally terminal, so `_wait_terminals` returns instantly and the basket short-circuits to CRITICAL | `Gateway pacing timeout` immediately followed by `BASKET_CRITICAL`; an order marked ERROR that IB later fills | certain |
| 11 | **P1** | `backend/app/services/kill_switch.py:297-302,311` | Process dies while a kill-switch flatten is running | `kill_switch_operations.status = 'FLATTENING'`; some positions flattened, some not | Account stays armed (`FLATTENING` is in `_ARMED_STATUSES`, `:51`), but nothing resumes the flatten. There is no reaper for `kill_switch_operations` and no endpoint that resumes one | A `FLATTENING` row with `updated_at` from before the restart, never advancing; open positions on an armed account; operator sees the kill switch as "in progress" indefinitely | certain |
| 12 | **P1** | Only check is `backend/app/services/order_manager.py:748`; absent from `backend/app/oms/coordinator.py:195,663,871` | Kill switch armed while a basket is mid-execution | Basket keeps submitting | The gate is evaluated once per account at fan-out. `BasketCoordinator.execute`, `_retry_incomplete` and `_compensate_filled` never re-check it, so an armed account can still receive new OPEN-side retry legs | `KILL_SWITCH_ACTIVATED` event followed by `AUTO_SQUARE_OFF_RETRY` on the same account | certain |
| 13 | **P2** | `backend/app/services/order_manager.py:186`; `deploy/systemd/trading-backend.service` `TimeoutStopSec=15`; `scripts/process_manager.py:120,674` | Session close at 16:00 ET, or a Gateway bounce triggering `_ensure_fastapi` | Basket killed mid-flight; job `PROCESSING`; claim `CLAIMED` | `worker_pool.stop()` cancels workers immediately; `has_in_flight_jobs()` (`worker_pool.py:79`) is never consulted at shutdown. Grace is 15 s (systemd) or 5 s (`_ensure_fastapi`) against a 90 s `fill_timeout` | `Restarting FastAPI so it can handshake with the new Gateway session` mid-session, then SIGKILL; job rows left `PROCESSING` with a live lease timestamp | certain |
| 14 | **P2** | `backend/app/broker/ibkr/gateway_rate_limiter.py:82,87-88,90` | Process restart | Both buckets initialised full (30 tokens) and `_cooldown_until = 0.0` | An Error 100 cooldown does not survive a restart, and a fresh process can emit a 30-message burst instantly | Error 100 shortly after a restart-and-resubscribe storm. Largely mitigated by `RestartSec=10` and a fresh IB socket, so this is a narrow window | speculative |
| 15 | **P2** | `backend/app/services/order_manager.py:91` used at `:891`; `backend/app/services/kill_switch.py:370,386` | Restart after a contract rollover | `_STK_CONTRACT_MONTH = "2026-09"` — the current month as of this review — and a separately hardcoded `"202612"` in the flatten path, in a different format | Neither is derived from anything; RMS check 4 (CONTRACT MONTH) validates against a literal | Kill-switch flatten legs carrying a contract month unrelated to the position being closed. See the question at the end — this may be deliberate for CFDs | certain (the literals) |
| 16 | **P2** | `backend/app/services/pnl.py:468-469`, resubscribe only at `:282-285` | Socket drops | `positions.live_pnl` frozen at its last value | `on_connection_closed` is `return`. `_resubscribe_all_active` only runs on IBKR error 1101, which requires a socket that recovers — and nothing reconnects the socket (finding 9) | Dashboard P&L stops moving with no error; `active_subscriptions` in `/demo/market-data-health` still non-zero | certain |
| 17 | **P2** | `backend/app/oms/coordinator.py:1146-1148,203` | Broker callback arrives before the first `execute()` of the process lifetime | Fill discarded | `self._loop` is set only inside `execute`, so `_on_broker_order_state` returns early for any callback before the first basket runs | `Failed to schedule broker snapshot persist` is *not* logged — the drop is silent | certain |
| 18 | **P1** | `backend/app/db/repositories/signal_repository.py:496-511` | A job reaches `attempt_count >= 3` with an expired lease while `PROCESSING` | `DEAD_LETTER` with `completed_at` set | The dead-letter arm matches `ACTIVE_LEASE_STATUSES`, which includes `PROCESSING`, and unlike `RecoveryManager` (`recovery.py:93`) it does **not** call `count_orders_emitted` first. A job that reached the broker is filed as terminal and never looked at again | `Stale lease sweep: … dead_lettered=1`; a `DEAD_LETTER` job whose `trade_id` has live `orders` rows | certain |
| 19 | **P2** | `backend/app/db/models/signal.py:60`; `backend/app/services/recovery.py:41-45,93,132-136` | A job is quarantined `RECOVERY_REQUIRED`, then the process restarts | Quarantine silently downgraded | `RECOVERY_REQUIRED` is not in `CLAIMABLE_STATUSES` so no worker touches it, and no HTTP route lists or resolves it. But `run_startup_recovery` re-reads it and requeues it to `QUEUED` whenever `count_orders_emitted` returns 0 — which is exactly the finding-1 window | `Recovery requeued job_id=… (no orders emitted)` for a job that was deliberately quarantined as "broker state unverified" | certain |
| 20 | **P2** | `deploy/systemd/process-manager.service` + `deploy/systemd/trading-backend.service` (both `WantedBy=multi-user.target`) + `deploy/systemd/trading-backend-restart.path` | Both units enabled on the same host | Two supervisors racing for `127.0.0.1:8001` | `process_manager.fastapi_cmd()` (`scripts/process_manager.py:360-365`) and `trading-backend.service` `ExecStart` launch the identical uvicorn command. `trading-backend.service` has `Restart=always` | Port-bind `EADDRINUSE` in `fastapi.log` or the journal; restart-budget exhaustion messages from the supervisor. See the question at the end | speculative |

---

## Detail on the load-bearing findings

### 1 — The submit/persist window (P0)

The basket submits a leg and only then writes the ledger row:

```266:275:backend/app/oms/coordinator.py
            order = await self._oms.submit_one_leg(
                intent,
                rms_result,
                index,
                oms_received_at=received_at,
                order_type=order_type,
            )
            submitted.append(order)
            self._order_baskets[order.internal_order_id] = basket
            await self._persist_child(order, intent, signal_pk=signal_pk, basket=basket)
```

`submit_one_leg` reaches `self._client.placeOrder(tws_order_id, contract, ib_order)` at
`backend/app/oms/ibkr_adapter.py:438`. `_persist_child` opens its own transaction
(`coordinator.py:1034`) and commits the `orders` row. Between those two points the order is
irreversibly at IB and invisible to the database.

Startup recovery then runs, in this order:

```77:80:backend/app/services/recovery.py
        async with self._session_factory() as session, session.begin():
            claim_stats = await ExecutionClaimRepository(session).reconcile_stale_claims(
                stale_after_sec=0.0
            )
```

`stale_after_sec=0.0` means *every* claim held by the dead process is evaluated. The
evaluation is:

```186:201:backend/app/db/repositories/execution_claim_repository.py
            emitted = await self.count_orders_emitted(claim.strategy_id, claim.signal_id)
            if emitted:
                await self.mark_executed(
                    ...
            else:
                await self.release(
                    claim.dedupe_key, note="Released by reconciliation: no orders emitted."
                )
```

`release` sets the claim `ABANDONED`, and `ABANDONED` is precisely the state the acquire
statement is allowed to retake:

```72:82:backend/app/db/repositories/execution_claim_repository.py
            .on_conflict_do_update(
                index_elements=["dedupe_key"],
                set_={
                    "state": CLAIM_STATE_CLAIMED,
                    ...
                },
                where=ExecutionClaimModel.state == CLAIM_STATE_ABANDONED,
            )
```

Then the job itself is requeued on the same evidence (`recovery.py:93`, `:132-136`). The
barrier the model docstring calls "the authoritative barrier" is lifted, the job is put back
on the queue, and the second attempt submits again. The 15 s reclaimer loop
(`worker_pool.py:139-141`) reaches the same conclusion via the same helper at
`stale_after_sec=300.0`, so this also happens without a restart.

The docstring at `backend/app/db/models/execution_claim.py:1-8` states the design intent
correctly; the implementation makes the barrier conditional on a row that is written after
the irreversible act.

### 2 — Operator and recovery close paths have no barrier at all (P0)

Three services call the coordinator directly, skipping `_acquire_execution_claim`
(`order_manager.py:1077`) entirely, and each mints a new identity per attempt:

```139:139:backend/app/services/position_close_service.py
            signal_id=f"CLOSEPAIR-{trade_id}-{uuid4().hex[:6]}",
```

```396:396:backend/app/services/kill_switch.py
                signal_id=f"KILLSWITCH-{pos.trade_id}-{uuid4().hex[:6]}",
```

```150:150:backend/app/services/broker_flatten_service.py
            signal_id=f"RECON-FLAT-{con_id}-{uuid4().hex[:6]}",
```

Because `execution_dedupe_key` is `f"{account}:{intent.strategy_id}:{intent.signal_id}"`
(`execution_claim_repository.py:207-215`), and because `baskets` is unique on
`(account_id, trade_id, action)` where `trade_id` is that same `signal_id`
(`coordinator.py:208`), no database constraint can relate two attempts at the same close.
The only dedupe is a module-level dict of in-flight asyncio tasks:

```33:33:backend/app/services/position_close_service.py
_IN_FLIGHT_PAIR_CLOSES: dict[tuple[int, str], asyncio.Task[ClosePairResponse]] = {}
```

which is gone the moment the process is. An operator who clicks "close pair", sees the
request hang because the backend died, and clicks again after the restart gets two market
closes on the same position.

### 8 — Disconnect converts live orders into a cleared latch (P0)

```1017:1030:backend/app/oms/ibkr_adapter.py
    def on_connection_closed(self) -> None:
        """Handle TWS connection dropped callback."""
        logger.warning("IBKRExecutionAdapter detected connection closed.")
        with self._lock:
            for order in self._orders_by_internal_id.values():
                if order.status not in (
                    OMSOrderStatus.FILLED,
                    OMSOrderStatus.CANCELLED,
                    OMSOrderStatus.REJECTED,
                    OMSOrderStatus.ERROR,
                ):
                    order.status = OMSOrderStatus.ERROR
                    order.error_message = "Connection closed unexpectedly"
                    self._notify_future_if_terminal(order)
```

A dropped API socket does not cancel working orders at IB. This treats "we lost the phone
line" as "the order failed". The resulting `ERROR` is then persisted and made permanent:

```108:115:backend/app/db/repositories/order_repository.py
        if existing is not None and existing.status in _TERMINAL_ORDER_STATUSES:
            persist_status = existing.status
            if existing.fill_qty is not None and existing.fill_qty > persist_filled:
                persist_filled = existing.fill_qty
```

with `_TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED", "ERROR"})` at
`:13`. A genuine fill arriving after reconnect cannot correct the row.

The basket goes CRITICAL and `CriticalRecoveryService` is scheduled. It decides what to
flatten from `fill_qty`:

```296:300:backend/app/services/critical_recovery.py
        for order in orders:
            if order.is_compensation:
                continue
            fill_qty = float(order.fill_qty or 0)
            if fill_qty <= _FILL_EPS:
                continue
```

Zero leftovers means:

```231:240:backend/app/services/critical_recovery.py
        leftovers = await self._collect_leftover_legs(basket_id)
        if not leftovers:
            detail = "No filled non-compensation legs; clearing latch."
            await self._clear_if_possible(
```

so the OPEN latch is released and trading resumes on an account that has a real position at
IB the ledger denies. The 30 s `PositionReconciler` will see it, but that service is
snapshot-and-log only — its module docstring at `position_reconciler.py:1` says so, and
`_persist_and_diff` writes rows and an event and nothing else.

### 4 — Double hydration inflates every RMS budget (P1)

`hydrate_runtime_from_db` accumulates rather than assigns:

```210:219:backend/app/services/order_manager.py
            open_rows = await PositionRepository(session).list_open()
            for row in open_rows:
                pos_key = (row.account_id, row.strategy_id)
                self._rms_context.open_positions[pos_key] = (
                    self._rms_context.open_positions.get(pos_key, 0) + 1
                )
                self._rms_context.processed_signals.add(
                    (row.account_id, row.strategy_id, row.trade_id)
                )
                self._add_row_exposure(row)
```

and `_add_row_exposure` (`:610-627`) is `get(key, 0) + notional` for both
`symbol_exposures` and `model_value_used`. Nothing clears these three dicts at the top of the
method — only `per_symbol_limits` is cleared, inside `_apply_symbol_limits` (`:247`).

It is called once from the lifespan:

```85:85:backend/app/main.py
            await order_manager.hydrate_runtime_from_db()
```

and again from the tail of startup recovery:

```148:149:backend/app/services/recovery.py
        if hasattr(self._order_manager, "hydrate_runtime_from_db"):
            await self._order_manager.hydrate_runtime_from_db()
```

`run_startup_recovery` returns early at `:55-57` only when there are neither pending jobs nor
pending baskets, so any restart with work in flight — exactly the restart where you most need
correct limits — doubles the counts. `processed_signals` is a set and is unaffected;
`model_value_used` self-heals on the first reconcile sweep via `_reseed_model_value_used`
(`order_manager.py:342`, reached from `after_reconcile_sweep` at `:337`). `open_positions`
and `symbol_exposures` are never rebuilt and stay doubled for the life of the process.

### 3 and 18 — the quarantine is advisory

The claim query deliberately picks up expired leases:

```380:386:backend/app/db/repositories/signal_repository.py
                (
                    (SignalJobModel.status.in_(CLAIMABLE_STATUSES))
                    | (
                        (SignalJobModel.status.in_(ACTIVE_LEASE_STATUSES))
                        & (SignalJobModel.lease_expires_at < now)
                    )
                )
```

`ACTIVE_LEASE_STATUSES` includes `PROCESSING` (`backend/app/db/models/signal.py:57`). The
reclaimer's stated policy is the opposite:

```488:492:backend/app/db/repositories/signal_repository.py
        A job that expired while still CLAIMED never began execution, so it is
        safe to requeue. A job that expired in PROCESSING may have already placed
        orders at the broker -- requeueing it blind would re-execute the signal,
        so it is quarantined as RECOVERY_REQUIRED for explicit reconciliation.
```

Workers poll every 0.5 s (`worker_pool.py:63`, `:160`); the reclaimer sweeps every 15 s
(`worker_pool.py:69`) and sleeps *before* its first sweep (`:127`). The worker reliably wins
the race, so the quarantine only ever applies to jobs no worker happened to pick up first.

Separately, the dead-letter arm of the same sweep has no ledger check:

```496:511:backend/app/db/repositories/signal_repository.py
        stmt_dead = (
            update(SignalJobModel)
            .where(
                SignalJobModel.status.in_(ACTIVE_LEASE_STATUSES),
                SignalJobModel.lease_expires_at < now,
                SignalJobModel.attempt_count >= max_attempts,
            )
            .values(
                status=JOB_STATUS_DEAD_LETTER,
```

Compare `RecoveryManager`, which checks `count_orders_emitted` *before* deciding
(`recovery.py:93-114`). A `PROCESSING` job on its third attempt that already reached the
broker is filed `DEAD_LETTER` with `completed_at` set and drops out of every recovery query.

### Retry wrappers, audited

- **`BasketCoordinator._retry_incomplete`** (`coordinator.py:663`) wraps *unfilled remainder*,
  not the original order. It cancels working orders first (`_cancel_working`, `:641`) and
  computes `remaining = float(orig_leg.quantity) - filled` (`:724`). Idempotent within a
  process via `self._retry_ids` (`:727-734`), which is in-memory and lost on restart — but the
  retry keys embed an attempt counter that also resets, so a restart mid-retry could re-issue
  the same remainder. Gated to paper ports only (`retry_policy.py:10-12`), so the live blast
  radius is limited today.
- **`CriticalRecoveryService._run_recovery`** (`critical_recovery.py:161`) retries the whole
  flatten up to `MAX_RECOVERY_ATTEMPTS = 2`. It re-snapshots the broker before and after each
  flatten (`:242`, `:257`) and skips legs whose snapshot quantity is ~0 (`:388-390`), so it is
  genuinely idempotent against broker state — *provided* `fill_qty` in the ledger is right,
  which findings 1 and 8 say it may not be.
- **`GatewayRateLimiter.acquire`** (`gateway_rate_limiter.py:136`) wraps token acquisition
  only, not the call. Safe.
- **`telegram.py:90,97`** retries notifications. Out of the money path.
- **`BrokerFlattenService.flatten_line`** (`broker_flatten_service.py:48`) coalesces concurrent
  callers onto one task but does not retry.

### Transaction / side-effect ordering, audited

- No IB order is submitted *inside* an open DB transaction. `_persist_child`
  (`coordinator.py:1034`), `_persist_basket` (`:1005`) and `_event` (`:1220`) each open and
  close their own `session.begin()`, and `submit_one_leg` runs between them, not within.
  Nothing rolls back an order.
- The commit boundary around **fill booking and exposure update is two transactions, and the
  exposure half is not a transaction at all.** `_persist_child` commits `orders` + `executions`
  together in one transaction (`coordinator.py:1034-1052`) — that pairing is atomic. But the
  exposure update (`_update_runtime_state`, `order_manager.py:1273`) is pure in-memory
  mutation of `RMSContext`, sequenced after the basket returns. A crash between them leaves
  the fills durable and the exposure unbooked; the only rebuild path is the double-counting
  hydration of finding 4.
- The **phantom-record** direction exists in the opposite place from usual: `_persist_inbound_signal`
  (`order_manager.py:1382`) commits a `signals` row plus two `event_log` rows before any
  execution, and `_acquire_execution_claim` (`:912`) deliberately commits before the submit —
  both are correct, and the claim's docstring explains why. The problem is not a phantom record;
  it is the *missing* record described in finding 1.

### Time handling, audited

This came out clean and is worth recording as a negative result. Every timestamp on the signal
path is constructed with `datetime.now(UTC)` — `worker_pool.py:307`, `signal_repository.py:110,318,362,437,472,494`,
`coordinator.py:262,881`, `ibkr_adapter.py:409,642,752,788`, `execution_claim_repository.py:58,176`.
All `DateTime` columns are `timezone=True` (e.g. `backend/app/db/models/signal.py:83-93`). The
one place a naive datetime could arrive is handled explicitly:

```385:387:backend/app/services/order_manager.py
        for ts, amount in existing:
            committed_at = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
```

Session-boundary logic lives entirely outside the app, in `America/New_York`
(`scripts/process_manager.py:94-96`, `deploy/systemd/trading-session-*.timer`), and the app
itself has no market-hours awareness. The only wall-clock coupling inside the app is the
hardcoded contract months in finding 15.

---

## Crash-point table

One row per hop of the signal path in `docs/review/MAP.md` §4. "Crash" means the process dies
immediately after that hop completes.

| Hop | What it is | Verdict | Why |
|---|---|---|---|
| 1 | HTTP receipt (`webhooks.py:167`) | **lossy** | Nothing persisted. TradingView gets no 202 and does not retry. |
| 2 | Validation (`webhooks.py:178-201`) | **lossy** | Same. Body is not yet on disk or in the DB. |
| 3 | Idempotency key (`worker_pool.py:28`) | **lossy** | Pure computation, nothing durable. |
| 4 | Enqueue `signal_jobs` (`signal_repository.py:332-335`) | **safe** | `ON CONFLICT DO NOTHING` on `idempotency_key`; a redelivery converges to the same row. Crash before commit is lossy but leaves no partial state. |
| 5 | Process boundary ingest→trading | **safe** | The row is the handoff. The trading process polls it; ingest holds no state. |
| 6 | Worker claim (`signal_repository.py:347`) | **safe** | Status `CLAIMED`, no side effect yet. `reclaim_stale_jobs` requeues `CLAIMED` (`:529-541`); startup recovery requeues it (`recovery.py:132`). Converges. |
| 7 | Payload parse (`worker_pool.py:309`) | **safe** | Status `PROCESSING`, still no side effect. Recovery finds zero orders emitted and requeues correctly. |
| 8 | Inbound `signals` row (`order_manager.py:668`) | **safe** | Upsert on `uq_signals_strategy_signal`; `event_log` rows carry idempotency keys (`:1401`, `:1414`). Replay converges. |
| 9 | Account fan-out (`order_manager.py:803`) | **safe** | No durable state yet; per-account coroutines are independent. |
| 10 | Pre-RMS gates (`order_manager.py:745-756`) | **safe** on the happy path, **dangerous** if the kill switch was armed and finding 5 fires — the replay is not gated. |
| 11 | RMS evaluation (`order_manager.py:1038`) | **safe** | Audit event is idempotency-keyed (`:1168`). But the replay re-evaluates against the doubled exposure of finding 4, so it may reject. |
| 12 | Instrument resolution (`order_manager.py:1046`) | **safe** | `instruments` upsert; `reqContractDetails` is a read. The what-if probe (`ibkr_adapter.py:367`) burns an order ID but sets `whatIf=True` and cancels in `finally` (`:377`). |
| 13 | Basket-critical gate (`order_manager.py:1057`) | **safe** | Read of an in-memory latch rehydrated from `baskets` (`coordinator.py:185-192`). |
| 14 | Execution claim (`order_manager.py:1077`) | **safe** | Committed in its own transaction. On restart the claim is `CLAIMED` with zero orders emitted, which is genuinely correct: release and requeue converge. |
| 15 | Gateway selection | n/a | Feature does not exist; single socket. |
| 16 | Basket row + `BASKET_CREATED` (`coordinator.py:219-252`) | **safe** | Upsert on `(account_id, trade_id, action)`; events idempotency-keyed. |
| **17a** | **`placeOrder` returns, `orders` row not yet written (`ibkr_adapter.py:438` → `coordinator.py:275`)** | **dangerous** | **Finding 1.** The order exists at IB; every recovery path reads zero orders emitted, abandons the claim, and requeues the job. Second submit. |
| 17b | `orders` row committed (`coordinator.py:275`) | **safe** | Claim is sealed `EXECUTED` by reconciliation (`execution_claim_repository.py:188`); job is quarantined `RECOVERY_REQUIRED` (`recovery.py:99`). No replay. Manual reconciliation still required, but nothing double-acts. |
| 18 | IB callbacks on the TWS thread | **lossy** | Callbacks after a restart find empty adapter maps and return (finding 6). Fills are lost to the ledger until an operator intervenes. |
| 19 | Fill booking (`coordinator.py:1019-1052`) | **safe** | `orders` upsert on `internal_order_id`, `executions` upsert on `exec_id`. Replay converges. |
| 20 | Position booking (`persistence.py:146`) | **safe** | One transaction covering `positions`, `signals`, `orders`, `event_log`; refuses to write unless both legs are `FILLED` (`:67`, `:73`). |
| 21 | Exposure update (`order_manager.py:1273`) | **lossy then wrong** | In-memory only. Lost on crash; rebuilt on restart by a hydration that double-counts (finding 4). |
| 22 | P&L | **lossy** | Realised P&L is recomputed from the ledger, so it is fine. Live P&L freezes after a disconnect and never resubscribes (finding 16). |
| 23 | Job terminal status (`worker_pool.py:384`) | **safe** | Fenced on `worker_id` (`signal_repository.py:452-457`). Crash before the write leaves `PROCESSING`, which recovery handles — subject to findings 3 and 18. |

Out-of-band paths, same treatment:

| Path | Verdict | Why |
|---|---|---|
| Operator square-off (`config.py:214` → `kill_switch.py:297`) | **dangerous** | Findings 2 and 11. No claim, fire-and-forget task, no durable resume. |
| Single-pair close (`config.py:322` → `position_close_service.py:60`) | **dangerous** | Finding 2. |
| Reconcile flatten (`reconcile.py:51` → `broker_flatten_service.py:65`) | **dangerous** | Finding 2. |
| Critical basket recovery (`critical_recovery.py:161`) | **safe** in isolation — it re-snapshots the broker before and after each flatten — but its input (`fill_qty`) is corrupted by findings 1 and 8. |
| Emergency kill-switch webhook (`emergency.py:76`) | **safe** | Arms only, submits no orders (`kill_switch.py:229`), DB write before cache write, idempotent on `_ARMED_STATUSES`. |
| Kill-switch clear (`kill_switch.py:105`) | **safe** | DB update first, cache discard second (`:117-132`) — fails in the safe direction. |

---

## Questions rather than findings

These look wrong but may be deliberate for reasons I cannot see in the code.

1. **`_STK_CONTRACT_MONTH = "2026-09"`** (`order_manager.py:91`) is this month, and the
   kill-switch flatten hardcodes `contract_month="202612"` in a different format
   (`kill_switch.py:370`, `:386`) while the single-pair close passes `""`
   (`position_close_service.py:106`). Is contract month load-bearing for CFDs at all, or is
   RMS check 4 a vestigial gate that these literals exist only to satisfy?

2. **Ingest is stopped at 16:00 ET** by both `trading-session-stop.service` and
   `process_manager.stop_children` (`scripts/process_manager.py:798-809`), so a TradingView
   alert outside 09:30–16:00 ET is refused at the TCP layer and lost. Is dropping out-of-hours
   alerts the intent, or should ingest stay up 24/7 (it is Postgres-only and has no IB
   dependency) and let the trading process decide?

3. **Two units both launch uvicorn on `:8001`** — `process-manager.service` via
   `fastapi_cmd()` and `trading-backend.service` directly, both `WantedBy=multi-user.target`,
   the latter with `Restart=always`. `docs/review/MAP.md` §8.7 flags the same ambiguity. Is
   only one of these enabled in production, and which one owns the restart decision?

4. **`worker_pool.has_in_flight_jobs()`** (`worker_pool.py:79`) exists and is documented as
   "True while any worker is inside `_execute_job` (live signal path)", but the only consumer
   is `MarginScanner` (`main.py:167`). Was it meant to gate shutdown, and is the 15 s
   `TimeoutStopSec` against a 90 s `fill_timeout` a deliberate "kill it and reconcile later"
   choice?

5. **The kill switch gates `OrderAction.OPEN` only** (`order_manager.py:748`). Letting CLOSE
   through on an armed account is presumably intentional. Is the same true for the OPEN-side
   retry legs that `_retry_incomplete` can issue after arming (finding 12), or is that an
   oversight?
