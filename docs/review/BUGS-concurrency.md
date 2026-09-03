# Concurrency, ordering and atomicity — review findings

**Scope:** duplicate-action windows, check-then-act, lock discipline, event-loop
integrity, rate limiting. Numeric correctness, restart behaviour and hygiene are out of
scope and appear only where a concurrency defect also has that face.

**Read at:** commit state of 2026-09-03. Paths are relative to `/home/tradingapp/app`,
so `backend/app/services/kill_switch.py:170` means
`/home/tradingapp/app/backend/app/services/kill_switch.py` line 170.

**Baseline taken from `docs/review/MAP.md`.** Its corrections hold and matter here: there
is exactly **one** `TWSClient` socket, **one** `GatewayRateLimiter` instance
(`backend/app/main.py:46`), **one** trading process, and no Redis under `backend/app/`.
Every "per-gateway" and "distributed" question in the brief collapses to "one process,
one event loop, one reader thread, plus a separate ingest process that only inserts into
`signal_jobs`".

**Two threads matter throughout.** The asyncio event loop of the `:8001` process, and the
`TWSClientThread` daemon reader thread started at
`backend/app/broker/ibkr/tws_client.py:552`. Wherever a finding says "TWS thread" it means
that thread, and the object being touched is reachable from both.

**What is already correct**, stated once so the findings below are not read as a blanket
indictment:

- Job claiming *is* atomic. `SignalJobRepository.claim_next_jobs`
  (`backend/app/db/repositories/signal_repository.py:347`) uses `FOR UPDATE SKIP LOCKED`
  (`:393`) inside the same transaction as the `UPDATE ... SET status='CLAIMED'` (`:401`).
  It is not a read-then-write.
- Terminal status writes and heartbeats are properly fenced on `worker_id`
  (`signal_repository.py:452-457`, `:474-481`). I traced the "lease expires mid-execution,
  another worker takes over" scenario end to end and the fence holds: the second worker
  rewrites `worker_id`, the first worker's `_write_status(PROCESSING, fence=True)`
  (`backend/app/services/worker_pool.py:302`) matches zero rows, sets `lease_lost` and
  returns before touching the broker.
- The `execution_claims` barrier is a genuine insert-or-retake in one statement with the
  `ON CONFLICT` arm restricted to `ABANDONED`
  (`backend/app/db/repositories/execution_claim_repository.py:59-84`). On the webhook path
  it is the thing that actually prevents double submission.
- `_exposure_guard` (`backend/app/services/order_manager.py:972`) sorts its keys with
  `sorted(keys, key=repr)` (`:995`) and `_reseed_model_value_used` (`:354`) uses the same
  total order, so the two cannot deadlock against each other. I checked for a reversed
  pair and there is none.

---

## Findings

| # | Severity | Location | Interleaving | What breaks | Observable symptom | Confidence |
|---|---|---|---|---|---|---|
| C1 | **P0** | `backend/app/services/kill_switch.py:170-215` | Two square-off POSTs; both `SELECT` for an active operation, both find none, both `INSERT` | No unique constraint on `kill_switch_operations`; the "strict idempotency" check is a plain check-then-act | Two `KILL_SWITCH_ACTIVATED` events, two flatten workers, every open pair reversed twice — account flips from flat to an equal and opposite position | **certain** |
| C2 | **P0** | `kill_switch.py:418`, `backend/app/services/position_close_service.py:180`, `backend/app/services/broker_flatten_service.py:183` | Operator closes a pair while the kill-switch flatten (or critical recovery) is flattening the same trade | All three call `BasketCoordinator.execute` directly, never `_acquire_execution_claim`; each mints a random `uuid4()` signal_id so no durable or in-memory key collides | Two independent MARKET reverses for one position; the second opens a new position in the opposite direction | **certain** |
| C3 | **P0** | `backend/app/services/order_manager.py:748` → `:1081` | Kill switch is armed after the OPEN passed the gate but before the basket submits | The gate is read exactly once per account, ~6 awaits and up to ~15 s before `placeOrder`; the flatten's position enumeration (`kill_switch.py:314-320`) already ran | Kill switch reports `COMPLETE` while a brand-new position sits open on a halted account | **certain** |
| C4 | **P1** | `backend/app/db/repositories/signal_repository.py:366-375` | Workers A and B run `claim_next_jobs` concurrently; B's `NOT EXISTS` sibling test cannot see A's uncommitted `status='CLAIMED'` | The documented OPEN-before-CLOSE serialisation (`:355-360`) is defeated by exactly the concurrency it exists to stop; the domain lock then orders them arbitrarily | CLOSE runs first, rejects with `NO_OPEN_POSITION`, job `REJECTED`; OPEN then runs and the position is stuck open with its CLOSE consumed | **likely** |
| C5 | **P1** | `order_manager.py:376-389` ← `backend/app/services/account_margin.py:272-274` | TWS thread replaces `margin_commitments[acct]` between the loop's `setdefault` and its `.append` | Cross-thread read-modify-write on `RMSContext` with no shared lock — the `__margin__` asyncio lock is invisible to the TWS thread | A margin commitment silently vanishes; the free-funds gate over-reports headroom and admits an OPEN it should reject | **certain** |
| C6 | **P1** | `backend/app/services/pnl.py:586-590` | Every settled OPEN calls `watch_open` on the loop, which calls the *blocking* `request_contract_details` | `time.sleep` in `blocking_acquire` (`tws_client.py:410`) plus `threading.Event.wait(3.0)` (`:433`) — up to ~6 s per leg, ~12 s per pair, on the event loop and inside the exposure guard | Lease heartbeats, `wait_for_terminal_or_fill` timeouts, the 30 s reconciler and all HTTP handlers stall in lockstep with every fill | **certain** |
| C7 | **P1** | `kill_switch.py:299-302` | Flatten task created, reference dropped, HTTP handler returns 202 | `asyncio.create_task` result is never stored and never awaited; the task is GC-eligible and its exception is never retrieved | Kill switch returns 202 and silently never flattens; no traceback anywhere | **certain** |
| C8 | **P2** | `pnl.py:435-436` vs `pnl.py:638-639` | TWS thread reroute handler and loop-side subscriber both do `req_id = self._next_req; self._next_req += 1` | Non-atomic increment across threads; two subscriptions collide on one reqId and overwrite `self._by_req[req_id]` | Ticks for one symbol booked as the mark of another; `positions.live_pnl` drifts against an unrelated instrument | **likely** |
| C9 | **P2** | `backend/app/broker/ibkr/gateway_rate_limiter.py:266-274` | Market-data resubscribe burst drains the normal bucket while a basket's leg 2 is waiting | `_try_consume_locked` distinguishes only priority 0; P1 order execution has no advantage over P3 market data, and waiters poll rather than queue | Leg 2 raises `GatewayPacingTimeout` after 8 s with leg 1 filled — a naked single leg, then compensation | **likely** |
| C10 | **P2** | `backend/app/oms/coordinator.py:1146-1152` | Every `orderStatus` / `execDetails` / `commissionReport` schedules an unbounded, uncoalesced persist coroutine | Each opens its own session; pool is 20+30 (`backend/app/db/session.py:33-36`) with `pool_timeout=30` — longer than the 30 s job lease | Under a fill burst, heartbeats time out on connection checkout and leases expire while workers are alive | **likely** |
| C11 | **P2** | `backend/app/services/worker_pool.py:252-255` | Heartbeat raises (pool timeout, transient DB error) rather than returning `False` | The `except Exception` arm logs and loops; it never sets `lease_lost`, unlike the `not renewed` arm at `:244` | Worker keeps placing orders on a lease the reclaimer already took; job lands in `RECOVERY_REQUIRED` with live orders | **certain** |
| C12 | **P2** | `backend/app/oms/ibkr_adapter.py:203-209` vs `tws_client.py:81-83` | TWS thread writes `next_order_id` while the loop is inside its read-increment-write | Two different locks: the counter is guarded by the adapter's lock but written by the client under none | Duplicate TWS order ids; `_orders_by_tws_id` entry overwritten and one order's callbacks applied to another | **speculative** |
| C13 | **P2** | `backend/app/services/model_blue/parser.py:140` + `backend/app/rms/models.py:116-120` | Two alerts for one symbol with different casing | `exposure_key` uses the raw leg symbol, which is `.strip()`ed but never upper-cased, while every reconcile/flatten path normalises | The two intents take *different* exposure locks and read *different* budget buckets — the money-per-symbol ceiling is silently doubled | **likely** |
| C14 | **P3** | `ibkr_adapter.py:621-625` | `asyncio.wait_for` cancels the future between the TWS thread's `fut.done()` test and the loop running the callback | Check-then-act across the thread boundary on future state | `InvalidStateError` in "Exception in callback" loop logs; no functional loss | **likely** |
| C15 | **P3** | `backend/app/oms/oms_service.py:176-179` | Any basket leg submission | `submit_one_leg` records `duplicate_key` but never rejects on it, unlike `submit_intent` (`:78-93`); `MAP.md` §5.4 lists it as a live guard | `_submitted_signals` is inert on the only production path; the real guard is the adapter map at `ibkr_adapter.py:397-401` | **certain** |

---

## C1 — Two square-off requests both arm and both flatten

`KillSwitchService.initiate_square_off` calls its idempotency check a "strict" one, but it
is a `SELECT` followed by an unconstrained `INSERT`:

```170:191:backend/app/services/kill_switch.py
            stmt = select(KillSwitchOperationModel).where(
                KillSwitchOperationModel.account_id == account_id,
                KillSwitchOperationModel.status.in_(
                    [
                        KILL_SWITCH_STATUS_ACTIVATING,
                        KILL_SWITCH_STATUS_FLATTENING,
                        KILL_SWITCH_STATUS_RECONCILING,
                        KILL_SWITCH_STATUS_RETRYING,
                    ]
                ),
            )
            result = await session.execute(stmt)
            existing_op = result.scalars().first()
            if existing_op is not None:
                ...
                return existing_op, False
```

and then, with nothing between it and the insert but a position query:

```202:218:backend/app/services/kill_switch.py
            operation = KillSwitchOperationModel(
                operation_id=uuid4(),
                account_id=account_id,
                ...
            )
            session.add(operation)

            # Block NEW opening signals for this account
            _arm_kill_switch_cache(account_id)
```

`KillSwitchOperationModel` (`backend/app/db/models/kill_switch.py:27-60`) has a UUID
primary key and plain indexes on `account_id` and `status`. There is **no** unique
constraint, partial or otherwise, that would make a second concurrent `ACTIVATING` row
fail. `MAP.md` §2.4 confirms `kill_switch_operations` carries no semantic constraint.

**Interleaving**

1. Operator clicks square-off; the dashboard proxy (`demo_streaming/api.py:303`) forwards
   it. A second click, a retry, or the watchdog safety action
   (`backend/app/services/watchdog/safety.py`) issues a second POST milliseconds later.
2. Request A opens a transaction, runs the `SELECT` at `:170`, finds no active operation.
3. Request B opens its own transaction, runs the same `SELECT`. Under READ COMMITTED it
   cannot see A's uncommitted row, so it also finds none.
4. A reads the open positions (`:194-200`), constructs the operation, `session.add`,
   commits.
5. B does the same and commits. Two `ACTIVATING` rows now exist for one account.
6. `config.py:213-214` — `if created_new: await kill_switch_svc.execute_flatten_operation_background(op.operation_id)` — fires for **both**, because both got `created_new=True`.
7. Two `_execute_flatten_operation` tasks each re-read `risk_state == "OPEN"` positions
   (`:314-320`). Neither has closed anything yet, so both see the same list.
8. Both build the same reverse legs (`:359-390`) and both call
   `baskets_coord.execute` (`:418`) with distinct `KILLSWITCH-{trade_id}-{uuid4}` ids.

**What breaks:** the flatten quantity is applied twice. Nothing downstream dedupes it —
see C2.

**Observable symptom:** account goes from long to short (or vice versa) by the original
position size instead of to flat; the reconciler reports `BROKER_ORPHAN` on the next
30 s sweep; `_reconcile_and_finalize` (`:469`) sees zero open ledger rows and marks the
operation `COMPLETE`.

---

## C2 — Every operator and recovery flatten path bypasses the durable dedupe barrier

`order_manager.py:1075-1077` describes the barrier's placement:

```1075:1077:backend/app/services/order_manager.py
        # Barrier goes up here: after every gate has passed, immediately before
        # anything can reach the broker.
        dedupe_key = await self._acquire_execution_claim(evaluated_intent)
```

That line is on the webhook path only. Three other producers reach `placeOrder` without
passing it:

| Path | Intent constructed | Submitted |
|---|---|---|
| Kill-switch flatten | `kill_switch.py:395-403` | `kill_switch.py:418` |
| Single-pair close | `position_close_service.py:138-146` | `position_close_service.py:180` |
| Reconcile / critical-recovery flatten | `broker_flatten_service.py:149-170` | `broker_flatten_service.py:183` |

All three mint a fresh identity per invocation:

```395:397:backend/app/services/kill_switch.py
            close_intent = OrderIntent(
                signal_id=f"KILLSWITCH-{pos.trade_id}-{uuid4().hex[:6]}",
                strategy_id=pos.strategy_id,
```

```138:140:backend/app/services/position_close_service.py
        close_intent = OrderIntent(
            signal_id=f"CLOSEPAIR-{trade_id}-{uuid4().hex[:6]}",
            strategy_id=strategy_id,
```

The `uuid4()` suffix defeats every downstream key at once: `execution_claims.dedupe_key`
is `f"{account}:{strategy_id}:{signal_id}"`
(`execution_claim_repository.py:207-215`), the `baskets` unique constraint is
`(account_id, trade_id, action)` with `trade_id = intent.signal_id`
(`coordinator.py:208`, `:1006-1009`), and `orders.internal_order_id` is derived from the
same signal_id (`oms_service.py:295-311`). Two flattens of one position collide on none
of them.

The only guards are two module-level dicts, each private to its own service and keyed
differently: `_IN_FLIGHT_PAIR_CLOSES` keyed `(account_id, trade_id)`
(`position_close_service.py:33`) and `_IN_FLIGHT_BROKER_FLATTENS` keyed
`(ibkr_account, con_id)` (`broker_flatten_service.py:34`). The kill-switch flatten has no
such dict at all. None of them observe the others.

**Interleaving**

1. A basket goes `CRITICAL`; `_fail_critical` (`coordinator.py:963`) schedules
   `CriticalRecoveryService.schedule_recovery` (`:995`).
2. Recovery snapshots the broker and calls `BrokerFlattenService.flatten_line` for the
   leftover conId (`critical_recovery.py:392`), which submits a MARKET reverse for the
   full broker quantity.
3. While that order is working at IB, the operator — seeing the position on the dashboard
   — POSTs `/config/accounts/{id}/positions/{trade_id}/close`.
4. `SinglePairCloseService._do_close_pair` reads `positions` (`:75`), which still shows
   the pair OPEN because nothing has booked the recovery fill yet, and builds reverse legs
   from `leg_a_signed_qty` / `leg_b_signed_qty` (`:96-124`).
5. `_IN_FLIGHT_PAIR_CLOSES` is empty for this key — the recovery flatten is tracked in a
   different dict — so the close proceeds.
6. Both orders execute. `is_open_blocked` does not apply: the basket-critical gate at
   `order_manager.py:1057-1060` is only consulted on the OPEN path.

**What breaks:** the position is reversed twice.

**Observable symptom:** broker shows a position of the same magnitude and opposite sign to
the original; `positions` shows `CLOSED`; the next reconcile sweep logs
`MISMATCH_BROKER_ORPHAN` with no ledger row to match it.

**Question rather than defect (please confirm):** is the bypass deliberate? An argument
exists for it — an operator hitting the emergency button should never be told "duplicate
execution, refusing" by a barrier row left over from a crashed attempt. If that is the
intent, the barrier is being traded away knowingly, but the *mutual* exclusion between the
three flatten producers still seems to be missing rather than chosen.

---

## C3 — Kill switch is read once, ~15 s before the order reaches IB

The gate lives in `_fanout_single_account`, before sizing is even finished:

```746:757:backend/app/services/order_manager.py
            intent = await handler.build_intent(signal, account=ctx)
            from app.services.kill_switch import is_account_kill_switch_active
            if intent.action == OrderAction.OPEN and is_account_kill_switch_active(ctx.account_id):
                logger.warning(
                    "KILL_SWITCH_ACTIVE: Blocking NEW open signal for account_id=%s ibkr=%s signal_id=%s",
                    ...
                )
                raise ValueError(
                    f"KILL_SWITCH_ACTIVE: Account {ctx.account_id} is in active emergency kill-switch mode."
                )
```

`is_account_kill_switch_active` is a set membership test on a process-local cache
(`kill_switch.py:60-62`). Because arming happens in the *same* process
(`_arm_kill_switch_cache` at `kill_switch.py:218` and `:286`, both reached from the
`:8001` routes), the value read is current at the instant it is read. Caching is not the
problem. **Re-reading is.** Between `:748` and the submit at `:1081` the following all
await, in order:

1. `RMSEngine.evaluate` + `_audit_rms` — one DB round trip (`:1038-1039`).
2. `_resolve_instruments` (`:1046`) → `ensure_cfd_instruments_for_symbols` →
   `reqContractDetails` against IB, off-loop via `to_thread`, 5 s default timeout, plus an
   `INSTRUMENT_RESOLVED` event write (`:1501`).
3. `_confirm_margin_if_borderline` (`:1050`) → a real `placeOrder(whatIf=True)` round trip
   with `margin_whatif_timeout_sec` = 5 s (`ibkr_adapter.py:367-369`).
4. `_acquire_execution_claim` (`:1077`) — its own transaction.
5. `BasketCoordinator.execute` (`:1081`) — `_persist_basket` plus two `event_log` writes
   before the first leg (`coordinator.py:219-252`).
6. `IBKRExecutionAdapter.submit_order` (`ibkr_adapter.py:391`) — the pacing acquire, up to
   `ibkr_gateway_max_wait_sec` = 8 s.

The gate is never consulted again on the way through. Neither is anything else durable:
the last thing checked before `placeOrder` is the in-memory `_critical` set
(`coordinator.py:88`).

**Interleaving**

1. Worker claims an OPEN job; the kill switch is clear; `:748` passes.
2. Instrument resolution issues `reqContractDetails` and blocks on the IB round trip.
3. Operator (or the watchdog) POSTs square-off. `initiate_square_off` commits an
   `ACTIVATING` row and calls `_arm_kill_switch_cache` (`kill_switch.py:218`).
4. `_execute_flatten_operation` reads `risk_state == "OPEN"` positions
   (`kill_switch.py:314-320`). The in-flight OPEN has no `positions` row yet — it is
   written by `persist_open` only after the basket settles — so it is **not** in the
   flatten set.
5. The flatten reverses everything it did see and `_reconcile_and_finalize` (`:469`) finds
   `net_unresolved == 0`, so the operation is marked `COMPLETE` (`:510`).
6. The original worker's contract details return. It resumes, takes the claim, submits,
   and the basket fills.
7. `_update_runtime_state` → `persist_open` writes a fresh `positions` row on an account
   that is armed and whose kill-switch operation says `COMPLETE`.

**What breaks:** the durable halt is advisory only past step 1 of the pipeline.

**Observable symptom:** a new open position on an account whose kill-switch status endpoint
(`config.py:276`) reports `kill_switch_active=true` and whose operation row reads
`COMPLETE` with `unresolved_count=0`. Nothing re-flattens it; only an operator clear
followed by a fresh square-off will.

---

## C4 — Sibling-trade_id exclusion does not survive concurrent claims

The claim query documents the guarantee it intends to provide:

```354:361:backend/app/db/repositories/signal_repository.py
        """Claim up to `limit` queued/expired jobs using FOR UPDATE SKIP LOCKED.

        Jobs sharing a ``trade_id`` are serialized: a candidate is skipped while
        any sibling on the same trade_id holds a live lease. Combined with the
        received_at ordering this makes an OPEN always execute before the CLOSE
        that follows it, instead of both being handed to workers at once and
        racing for the domain lock.
        """
```

The mechanism is an `EXISTS` correlated subquery over an alias:

```365:395:backend/app/db/repositories/signal_repository.py
        sibling = aliased(SignalJobModel)
        sibling_in_flight = (
            select(literal(1))
            .select_from(sibling)
            .where(
                sibling.trade_id == SignalJobModel.trade_id,
                sibling.job_id != SignalJobModel.job_id,
                sibling.status.in_(ACTIVE_LEASE_STATUSES),
            )
            .exists()
        )
        ...
            .with_for_update(skip_locked=True, of=SignalJobModel)
```

`of=SignalJobModel` locks only the candidate row. The sibling rows read by the `EXISTS`
are not locked and are evaluated against the statement snapshot, so another transaction's
uncommitted `status='CLAIMED'` is invisible to them. `ACTIVE_LEASE_STATUSES` is
`(CLAIMED, PROCESSING)` (`backend/app/db/models/signal.py:57`), and a job that is still
`QUEUED` in your snapshot does not match.

**Interleaving** (worker A and worker B, both idle, both polling every 0.5 s at
`worker_pool.py:160`; jobs J-open and J-close both `QUEUED` for trade `T`):

1. A begins its transaction and runs the `SELECT`. J-open sorts first by `received_at`.
   Its `EXISTS` finds no sibling in `(CLAIMED, PROCESSING)`, so J-open is a candidate.
   A takes the row lock.
2. B begins its transaction and runs the same `SELECT`. J-open's row is locked, so
   `SKIP LOCKED` skips it. B evaluates J-close: its `EXISTS` looks at J-open, whose
   committed status is still `QUEUED` — A has not committed. Not a live sibling.
3. B takes the lock on J-close and both transactions `UPDATE ... status='CLAIMED'` and
   commit.
4. A calls `_process_claimed_job` and awaits `self._get_domain_lock(job.account_scope, job.strategy_id)` (`worker_pool.py:181`), then `async with domain_lock` (`:195`).
   B does the same. Both keys are `("default", "model_blue")` — `account_scope` is never
   set by the ingest path (`webhooks.py:250` omits it, defaulting to `None` at
   `signal_repository.py:312`).
5. Whichever coroutine reaches `:195` first wins. Nothing orders them by `received_at`.
6. B wins. `_execute_job` → `ModelBlueStrategy._build_close_intent`
   (`backend/app/services/model_blue/strategy.py:220`) calls
   `self._trades.get(trade_id, account_id=...)`, which reads `positions`. The OPEN has not
   run, so there is no row.
7. `:222-226` raises `NO_OPEN_POSITION`. The fan-out turns it into `all_rejected`, and
   `worker_pool.py:356` writes `REJECTED` — a **terminal** status.
8. A releases the domain lock; the OPEN executes normally and books a position.

**What breaks:** the CLOSE is consumed and terminally rejected. `signal_jobs.idempotency_key`
is unique (`signal.py:75`) and `create_job_if_not_exists` is `ON CONFLICT DO NOTHING`
(`signal_repository.py:335`), so a re-sent CLOSE webhook returns 202 and enqueues nothing.

**Observable symptom:** position stays open indefinitely with a `REJECTED` job carrying
`NO_OPEN_POSITION` in `last_error`, and a corresponding `signals` row with status
`REJECTED`. Only a manual close clears it.

**Note on the window's size.** This requires both jobs to be `QUEUED` simultaneously and
two workers to poll in the same instant. With ten workers on a 0.5 s poll and OPEN/CLOSE
pairs that arrive close together, it is not remote. It is `likely` rather than `certain`
only because I have not reproduced it against Postgres.

---

## C5 — RMS margin state is mutated from the TWS reader thread

`add_snapshot_listener` is wired at `order_manager.py:174`, and the callback runs wherever
`AccountMarginService` publishes:

```272:279:backend/app/services/account_margin.py
            for callback in list(self._on_snapshot):
                try:
                    callback(snap)
                except Exception:
                    logger.exception(
                        "Account margin snapshot listener failed account=%s",
                        snap.ibkr_account,
                    )
```

`on_account_summary_end` is an `EWrapper` listener dispatched from
`tws_client.py:252-261`, so this loop runs on `TWSClientThread`. The callback rewrites
shared RMS state:

```376:389:backend/app/services/order_manager.py
    def _on_margin_snapshot(self, snapshot) -> None:
        """Broker snapshot overwrites the tally for commitments older than as_of - GRACE."""
        key = str(snapshot.ibkr_account).strip().upper()
        self._rms_context.margin_snapshots[key] = snapshot
        existing = self._rms_context.margin_commitments.get(key, [])
        ...
        self._rms_context.margin_commitments[key] = kept
```

The event loop writes the same structure with a read-modify-write:

```404:406:backend/app/services/order_manager.py
        self._rms_context.margin_commitments.setdefault(account, []).append(
            (datetime.now(UTC), signed)
        )
```

and reads it in the gate:

```428:429:backend/app/services/order_manager.py
        commitments = self._rms_context.margin_commitments.get(account, [])
        effective = effective_free_margin(snapshot, commitments, policy)
```

The `("__margin__", account)` key taken by `_exposure_guard` (`:990-991`) is an
`asyncio.Lock`. It serialises loop tasks against each other and means nothing to the TWS
thread.

**Interleaving**

1. Loop, inside `_commit_margin`, evaluates `setdefault(account, [])` and obtains a
   reference to list `L`.
2. Interpreter yields to the TWS thread (bytecode boundary — `setdefault` and `append` are
   separate operations).
3. TWS thread runs `_on_margin_snapshot`, builds `kept` (a new list), and executes
   `self._rms_context.margin_commitments[key] = kept`. The dict entry now points at a
   different list.
4. Loop resumes and appends the commitment to `L`, which is no longer reachable from the
   dict.
5. Next OPEN on that account calls `_assert_account_has_free_margin`, reads `kept`, and
   does not see the commitment.

**What breaks:** `effective_free_margin` is computed from an under-counted commitment
tally. The gate's whole purpose (`RMSContext` docstring, `rms/models.py:184-185`: "Bridges
the ~3 minute accountSummary gap") is to cover exactly the interval in which this is lost.

**Observable symptom:** an OPEN admitted on an account with no real headroom; IB rejects
with a margin error, or the fill succeeds and the account is over-committed until the next
`accountSummary` push corrects the snapshot.

A second, simpler face of the same defect: `_assert_account_has_free_margin` runs at
`:745` — **outside** `_exposure_guard`, which is only entered at `:1016`. So even
loop-only concurrency has the margin check and the margin commit under different locks.
The worker-pool domain lock masks this for same-strategy signals, which is why I have not
filed it separately.

---

## C6 — A blocking IB round trip runs on the event loop, inside the exposure guard

`LivePnlService._request_ticks` qualifies the contract synchronously:

```584:590:backend/app/services/pnl.py
        # Live IBKR Contract Qualification if connected
        is_conn = getattr(self._client, "is_connected", None)
        if callable(is_conn) and is_conn():
            req_details = getattr(self._client, "request_contract_details", None)
            if callable(req_details):
                try:
                    details = req_details(contract, timeout=3.0)
```

`TWSClient.request_contract_details` is the blocking variant. The async wrapper
`request_contract_details_async` exists two lines below it (`tws_client.py:445-449`, and
`cfd_discover.py:132-136` uses it correctly), but this call site does not. What it does
instead:

```410:414:backend/app/broker/ibkr/tws_client.py
            acquired = self._rate_limiter.blocking_acquire(
                PRIORITY_CONTRACT_DETAILS,
                "reqContractDetails",
                timeout=min(timeout, self._rate_limiter.max_wait_sec),
            )
```

`blocking_acquire` sleeps with `time.sleep(sleep_for)` (`gateway_rate_limiter.py:227`),
and the wait for the reply is `completed = event.wait(timeout=timeout)`
(`tws_client.py:433`) — a `threading.Event`, not an asyncio one.

The caller chain is entirely on the loop: `order_manager.py:1109-1110`
(`self._live_pnl.watch_open(filled_intent)`) → `pnl.py:160`
(`self._request_ticks(intent.account_id, key[1], leg)`) → `pnl.py:590`. And
`order_manager.py:1110` sits inside `async with self._exposure_guard(intent)` opened at
`:1016`, so the symbol locks are held for the duration too.

**Interleaving**

1. A pair OPEN fills. `_evaluate_and_submit_locked` reaches `:1109` still holding the
   `(account, LEG_A)`, `(account, LEG_B)`, `__margin__` and `__model_value__` locks.
2. `watch_open` iterates the two legs; for each, `_request_ticks` blocks the OS thread for
   up to 3 s in `blocking_acquire` plus up to 3 s in `event.wait`.
3. For ~6–12 s **nothing else in the process runs**: not the other nine worker tasks, not
   the lease heartbeat tasks (interval 10 s, lease 30 s — `worker_pool.py:233`, `:60`),
   not the reconciler, not `asyncio.wait_for` timeout machinery, not the
   `loop.call_soon_threadsafe` callbacks the TWS thread is queueing to resolve fill
   futures (`ibkr_adapter.py:625`).
4. The loop resumes with a backlog: every `wait_for` deadline is measured against wall
   clock, so timeouts that expired during the stall fire immediately.

**What breaks:** `wait_for_terminal_or_fill` (`ibkr_adapter.py:517`) can report a timeout
for an order that the TWS thread already resolved, because the resolving callback was
sitting in the loop's ready queue. `coordinator.py:612-618` then logs "Timed out waiting
for terminal state" and `_basket_complete` (`:586`) is evaluated on stale `OMSOrder`
state, which routes an actually-filled basket into the UNWINDING/compensation branch at
`:391`.

**Observable symptom:** spurious `BASKET_UNWINDING` on baskets whose legs both filled, and
correlated bursts of heartbeat-renewal latency in the logs whenever a pair opens. Same
path runs at startup for every open position via `hydrate_from_position_rows`
(`pnl.py:169` → `:193` → `watch_open`).

---

## C7 — The emergency flatten is a fire-and-forget task with no reference held

```297:302:backend/app/services/kill_switch.py
    async def execute_flatten_operation_background(self, operation_id: UUID) -> None:
        """Trigger background worker task to execute non-blocking position flattening."""
        asyncio.create_task(
            self._execute_flatten_operation(operation_id),
            name=f"kill-switch-flatten-{operation_id}",
        )
```

The `Task` is not stored, not awaited, and has no done-callback. Two consequences, both
documented asyncio behaviour: the loop holds only a weak reference to a scheduled task, so
it can be garbage-collected mid-flight; and if it raises, the exception is retrieved by
nobody, surfacing at best as a "Task exception was never retrieved" warning during
finalisation.

Compare `CriticalRecoveryService.schedule_recovery` (`critical_recovery.py:116-137`),
which does it correctly — stores the task in `self._in_flight`, and attaches a `_done`
callback that calls `t.exception()`.

**Interleaving**

1. `config.py:214` awaits `execute_flatten_operation_background`, which returns as soon as
   the task is scheduled.
2. The HTTP handler returns 202 with `status=ACTIVATING`.
3. The task's only strong reference was the local in step 1, now out of scope.
4. `_execute_flatten_operation` awaits its first DB call (`:308`) and suspends. If a GC
   cycle collects it here, or if any of the `asyncio.gather` legs at `:344` raises past
   the `return_exceptions=True` boundary, nothing observes it.

**What breaks:** the kill switch's durable state says `ACTIVATING`/`FLATTENING` forever
while no orders were sent. The account is armed (so no new OPENs) but not flat.

**Observable symptom:** `kill_switch_operations` row stuck in `ACTIVATING` with
`flattened_count=0`, positions still open at the broker, and no `KILL_SWITCH_ACTIVATED`
event in `event_log`.

The same anti-pattern, lower stakes, at `coordinator.py:1150`
(`asyncio.run_coroutine_threadsafe(...)` future discarded — persist failures invisible),
`pnl.py:751` (same), and `pnl.py:784-789`:

```784:789:backend/app/services/pnl.py
                    loop.call_later(
                        wait,
                        lambda ak=account_id, tid=trade_id: asyncio.create_task(
                            self._schedule_persist(ak, tid)
                        ),
                    )
```

---

## C8 — `LivePnlService._next_req` is incremented from two threads

Loop side, in `_request_ticks`:

```638:643:backend/app/services/pnl.py
        req_id = self._next_req
        self._next_req += 1
        self._by_req[req_id] = (account_id, trade_id, leg.symbol)
        self._contract_reqs[c_key] = req_id
        self._req_to_contract[req_id] = c_key
        self._listeners_by_req.setdefault(req_id, set()).add((account_id, trade_id, leg.symbol))
```

TWS-thread side, in the `rerouteMktDataReq` handler (dispatched from
`tws_client.py:198-207`):

```434:443:backend/app/services/pnl.py
        if new_c_key not in self._contract_reqs:
            new_req_id = self._next_req
            self._next_req += 1
            listeners = self._listeners_by_req.get(reqId, set())
            mapped = self._by_req.get(reqId)
            if mapped:
                self._by_req[new_req_id] = mapped
            self._contract_reqs[new_c_key] = new_req_id
            self._req_to_contract[new_req_id] = new_c_key
            self._listeners_by_req[new_req_id] = set(listeners)
```

`self._next_req += 1` is a load-add-store, not atomic. `LivePnlService` owns a
`threading.Lock` (`pnl.py:104`) but it is used only for the persist bookkeeping
(`_schedule_persist`, `:766`), never for the request-id maps.

**Interleaving**

1. Loop reads `self._next_req` = 50007 for leg A of a new pair.
2. TWS thread, handling a CFD reroute, reads `self._next_req` = 50007.
3. Both store 50008. Both proceed to use 50007 as their reqId.
4. `self._by_req[50007]` and `self._req_to_contract[50007]` are written twice; the second
   write wins.
5. `reqMktData(50007, ...)` is issued twice against IB for two different contracts.

**What breaks:** `on_tick_price` (`pnl.py:350`) resolves the listener set from
`self._listeners_by_req[reqId]` / `self._by_req[reqId]`, so ticks for whichever contract IB
answers get booked as the mark of the other symbol.

**Observable symptom:** `positions.live_pnl` for one pair tracking an unrelated
instrument's price; `/demo/market-data-health` shows a `LIVE` contract whose `last_mark`
does not match the symbol.

---

## C9 — Priority is only honoured for emergency flatten; there is no queue

```259:274:backend/app/broker/ibkr/gateway_rate_limiter.py
    def _try_consume_locked(self, priority: int) -> bool:
        now = time.monotonic()
        if self._in_cooldown_locked(now):
            return False
        self._refill_locked(now)
        if self._global_tokens < 1.0:
            return False
        is_emergency = priority == PRIORITY_EMERGENCY_FLATTEN
        if is_emergency:
            self._global_tokens -= 1.0
            return True
        if self._normal_tokens < 1.0:
            return False
        self._global_tokens -= 1.0
        self._normal_tokens -= 1.0
        return True
```

Priorities 1 through 4 are indistinguishable: `PRIORITY_ORDER_EXECUTION` (1),
`PRIORITY_CONTRACT_DETAILS` (2), `PRIORITY_MARKET_DATA` (3) and `PRIORITY_DIAGNOSTIC` (4)
all take one global and one normal token on identical terms. Priority is recorded in
metrics (`_record_acquire_locked`, `:235-237`) and used for the reserve slice, nothing
else.

Nor is there a waiting queue. Every blocked caller polls:

```186:190:backend/app/broker/ibkr/gateway_rate_limiter.py
            sleep_for = min(wait_sec, remaining, 0.05)
            if sleep_for > 0:
                delayed = True
                await asyncio.sleep(sleep_for)
                total_waited += sleep_for
```

Grants therefore go to whoever happens to re-test first after a refill, not to whoever
waited longest.

To answer the brief's question directly: the bucket state **is** genuinely shared. There is
one `GatewayRateLimiter` constructed at `main.py:46`, handed to both the adapter
(`main.py:60`) and the client (`main.py:53`), and guarded by a `threading.Lock`
(`:84`) that is correctly held across the token test and decrement and correctly released
before sleeping. There is no per-worker copy and no N-workers-each-burst-to-the-limit
problem. The failure mode is starvation, not over-issuance.

**Interleaving**

1. `PositionReconciler` sweep, `hydrate_live_pnl`, or a CFD reroute storm issues a batch of
   `reqMktData` / `reqContractDetails` at priorities 2–3.
2. `normal_msg_per_sec` is 24/s with a burst cap of `min(30, 24) = 24`
   (`gateway_rate_limiter.py:86`). The batch drains it.
3. A basket is mid-submit. Leg 0 got its token at `ibkr_adapter.py:391` and is working at
   IB. `coordinator.py:263-273` loops to leg 1 and calls `submit_one_leg` → `submit_order`
   → `_acquire_for_order`.
4. Leg 1 competes on equal terms with the market-data backlog, re-testing every 50 ms.
5. At 8 s (`ibkr_gateway_max_wait_sec`), `acquire` raises `GatewayPacingTimeout`. Caught at
   `ibkr_adapter.py:261-264`, which sets the order to `ERROR` and returns it.
6. `coordinator.py:324-325` sets `abort_remaining = True`. The basket is incomplete.

**What breaks:** a pair with one live leg. The unwind path at `:391-451` then has to
compensate, itself competing for the same tokens.

**Observable symptom:** `BASKET_UNWINDING` → `COMPENSATED` (or `CRITICAL`) with
`error_message="Gateway pacing timeout"` on the second leg, concentrated at market open
when reconnect resubscribes market data.

**Question rather than defect:** is the flat P1–P4 treatment deliberate? Giving order
submission the same footing as market data may be a decision to avoid ever letting
diagnostics be indefinitely postponed. If not, `PRIORITY_ORDER_EXECUTION` currently buys
nothing.

---

## C10 — Uncoalesced persist scheduling from the TWS thread can exhaust the pool

```1146:1154:backend/app/oms/coordinator.py
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._persist_broker_snapshot(order, kind), loop
            )
        except Exception:
            logger.exception("Failed to schedule broker snapshot persist")
```

`_on_broker_order_state` is invoked from `_emit_order_state` (`ibkr_adapter.py:123-128`)
for `BROKER_ACK`, `PARTIAL_FILL`, `FILL`, `COMMISSION`, `REJECTED`, `CANCELLED` and
`ERROR`. Each scheduled coroutine calls `_persist_child` (`coordinator.py:1184`), which
opens its own session and transaction (`:1034`) and may additionally open a nested
`_ensure_signal_pk` path (`:1036`, `:1054`).

There is no coalescing and no cap. The upsert itself is safe — `record_oms_order` is
`INSERT ... ON CONFLICT DO UPDATE` on `internal_order_id`
(`backend/app/db/repositories/order_repository.py:157-164`) so concurrent writers serialise
on the row lock rather than colliding. The problem is connection demand.

**Interleaving**

1. A multi-leg basket fills in many small executions. IB delivers `execDetails` +
   `commissionReport` + `orderStatus` per fragment.
2. Each callback synchronously schedules a persist coroutine onto the loop.
3. Ten worker tasks are already holding sessions in `_execute_job`, each with a heartbeat
   task that opens its own session every 10 s (`worker_pool.py:239`).
4. Pool is `pool_size=20, max_overflow=30` (`db/session.py:33-34`) — 50 total.
5. Persist coroutines saturate it. New checkouts queue against `pool_timeout=30`
   (`:35`).
6. A heartbeat's checkout takes longer than the 30 s lease (`worker_pool.py:60`). It
   eventually raises rather than returning `False`, which lands in C11's swallowing arm.

**What breaks:** lease liveness is coupled to persist throughput.

**Observable symptom:** `"Worker %s failed to renew heartbeat for job %s"` warnings during
fill bursts, followed by `"Stale lease sweep: requeued=… quarantined=…"` from the
reclaimer for jobs whose workers are still running.

---

## C11 — Heartbeat failure by exception does not set `lease_lost`

```234:255:backend/app/services/worker_pool.py
        while not cancel_event.is_set():
            try:
                await asyncio.sleep(interval)
                if cancel_event.is_set():
                    break
                async with self._session_factory() as session, session.begin():
                    repo = SignalJobRepository(session)
                    renewed = await repo.heartbeat_lease(
                        job_id, worker_id, lease_duration_sec=self._lease_duration_sec
                    )
                if not renewed:
                    lease_lost.set()
                    logger.error(
                        "Worker %s LOST its lease on job %s -- another worker may now own it",
                        worker_id,
                        job_id,
                    )
                    break
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.warning("Worker %s failed to renew heartbeat for job %s", worker_id, job_id)
```

The `not renewed` branch is the "someone took my lease" signal and it sets the flag. The
`except Exception` branch is the "I could not tell whether I still have my lease" case and
it sets nothing — it logs and loops back into another 10 s sleep.

**Interleaving**

1. Worker holds a job in `PROCESSING`, lease expires at T+30.
2. At T+10 and T+20 the heartbeat's session checkout times out (see C10) and raises. The
   flag stays clear.
3. At T+30 the lease expires. At the next 15 s reclaimer tick, `reclaim_stale_jobs`
   matches the `PROCESSING` + expired branch (`signal_repository.py:513-527`) and writes
   `RECOVERY_REQUIRED`, `worker_id=None`.
4. The worker, unaware, continues through `_evaluate_and_submit` and places real orders.
5. It finishes, checks `lease_lost.is_set()` at `worker_pool.py:327` — still clear — and
   calls `_write_status(COMPLETED, fence=True)`.
6. The fence at `signal_repository.py:456-457` requires `worker_id == worker-N` **and**
   status in `ACTIVE_LEASE_STATUSES`. The row is `RECOVERY_REQUIRED` with a null worker, so
   zero rows match. `_write_status` sets `lease_lost` at `:277` and returns `False`.

**What breaks:** nothing duplicates — `RECOVERY_REQUIRED` is in neither
`CLAIMABLE_STATUSES` nor `ACTIVE_LEASE_STATUSES`, so no second worker picks it up, and the
fence prevents the stale write. The loss is observability: a completed execution is filed
as needing manual reconciliation, and the deliberate "leave it for recovery" log at
`:330-334` never fires because the flag was set too late.

**Observable symptom:** `RECOVERY_REQUIRED` jobs whose `last_error` reads "Worker lease
expired mid-execution; broker state unverified" but whose orders all show `FILLED` and
whose positions booked correctly. Operators cannot distinguish these from genuine
mid-execution deaths.

---

## C12 — `next_order_id` is guarded by the adapter's lock but written by the client under none

Reader-incrementer, on the loop, under `IBKRExecutionAdapter._lock`:

```201:209:backend/app/oms/ibkr_adapter.py
    def _get_next_tws_order_id(self) -> int:
        """Reserve and increment the next valid order ID from TWS under lock."""
        with self._lock:
            current_id = self._client.next_order_id
            if current_id is None:
                current_id = 1
                self._client.next_order_id = 1
            self._client.next_order_id = current_id + 1
            return current_id
```

Writer, on the TWS thread, under nothing:

```76:87:backend/app/broker/ibkr/tws_client.py
    def nextValidId(self, orderId: int) -> None:
        """Callback received when initial handshake finishes.

        Indicates the connection is ready to accept commands.
        """
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self._connected_event.set()
```

`connectionClosed` (`tws_client.py:155`) and `disconnect_clean` (`:586`) also write it from
their respective threads.

I am labelling this **speculative** because `MAP.md` §Corrections records that there is no
reconnect-on-drop loop and `connect_and_start` runs once at `main.py:105`, so in the
current topology `nextValidId` should arrive exactly once, before any order is placed. The
hazard is latent: it becomes real the moment a reconnect path is added, or if the gateway
pushes a second `nextValidId`. If it fires, the counter is reset to a value already in use,
`self._orders_by_tws_id[tws_order_id]` (`ibkr_adapter.py:412`) is silently overwritten, and
`orderStatus`/`execDetails` for one order are applied to another.

---

## C13 — The exposure lock key is not case-normalised at its source

`exposure_key` passes the symbol through untouched:

```116:120:backend/app/rms/models.py
def exposure_key(intent: OrderIntent, symbol: str) -> str | tuple[int, str]:
    """Account-scoped symbol exposure key when account_id is present."""
    if intent.account_id is not None:
        return (intent.account_id, symbol)
    return symbol
```

and the symbol arrives from the webhook stripped but not upper-cased:

```140:143:backend/app/services/model_blue/parser.py
        symbol = str(raw_leg.get("underlying") or bucket.get("underlying") or "").strip()
        if not symbol:
            raise ModelBlueValidationError(
                f"MODEL_BLUE_MISSING_SYMBOL: buckets[{index}] has no underlying/symbol."
```

Every other symbol consumer in the system normalises: `position_reconciler._norm_symbol`
(`:79-80`), `critical_recovery._norm_symbol` (`:61-62`),
`broker_flatten_service.py:99-100`, `ibkr_adapter._managed_accounts_set` for accounts
(`:141`). The exposure path does not, and the `__margin__` key one line away in the same
function *does* (`order_manager.py:991`: `intent.ibkr_account.strip().upper()`).

This is the brief's "a lock keyed on `NIFTY` and `nifty` is no lock at all" case, and it is
worse than a lock miss: the same un-normalised string is the key for `symbol_exposures`
(`order_manager.py:1289-1293`), which `MoneyPerStockCheck` reads
(`backend/app/rms/checks/money_per_stock.py:67-70`), and for `per_symbol_limits`
(`:250`).

**Interleaving**

1. Alert 1 arrives with `"underlying": "AAPL"`; alert 2, a different `trade_id`, with
   `"underlying": "aapl"`.
2. Both are claimed and serialised by the domain lock (same strategy), so they run one
   after the other — the lock miss alone is not yet visible.
3. Intent 1 books exposure under `(7, "AAPL")` at `order_manager.py:1290`.
4. Intent 2's `MoneyPerStockCheck` reads `context.symbol_exposures.get((7, "aapl"), Decimal(0))`
   → zero. It passes against a fresh budget.
5. Under two *different* strategies (different domain locks) steps 3 and 4 interleave
   concurrently, and the exposure guard does not serialise them either, because
   `_exposure_guard` acquired `(7, "AAPL")` and `(7, "aapl")` — two distinct
   `asyncio.Lock` objects out of `self._exposure_locks`.

**What breaks:** the per-symbol money ceiling. Also `per_symbol_limits.get((account_id, symbol))`
misses, falling through to `default_symbol_limits` or the strategy default of
`Decimal(10_000_000)` (`order_manager.py:1448`).

**Observable symptom:** aggregate exposure on one symbol exceeding its configured limit,
with two `symbol_exposures` buckets differing only in case. `likely` rather than `certain`
because it depends on TradingView alert text being inconsistent; I have not confirmed the
alert templates.

---

## C14 — `set_result` on a future the loop may have already cancelled

```613:625:backend/app/oms/ibkr_adapter.py
    def _notify_future_if_terminal(self, order: OMSOrder) -> None:
        """Resolve waiting future if order reached a terminal state."""
        if order.status in (
            OMSOrderStatus.FILLED,
            OMSOrderStatus.CANCELLED,
            OMSOrderStatus.REJECTED,
            OMSOrderStatus.ERROR,
        ):
            fut_tuple = self._fill_futures.pop(order.internal_order_id, None)
            if fut_tuple:
                fut, loop = fut_tuple
                if not fut.done():
                    loop.call_soon_threadsafe(fut.set_result, order)
```

`fut.done()` is tested on the TWS thread; `fut.set_result` runs later, on the loop. In
between, the waiter can time out:

```540:547:backend/app/oms/ibkr_adapter.py
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            with self._lock:
                self._fill_futures.pop(internal_order_id, None)
```

`asyncio.wait_for` cancels the inner future on timeout, so `set_result` on the loop raises
`InvalidStateError` from a `call_soon_threadsafe` callback with no handler.

**Observable symptom:** `Exception in callback Future.set_result(...)` /
`InvalidStateError: invalid state` in the loop's default exception handler, correlated with
`"Timed out waiting for terminal state"` from `coordinator.py:612`. No state is corrupted —
the timeout path re-reads the live order at `:544-546`.

---

## C15 — `submit_one_leg` records the duplicate key but never enforces it

```176:179:backend/app/oms/oms_service.py
        duplicate_key = f"{intent.account_id}:{intent.signal_id}"
        if duplicate_key not in self._submitted_signals:
            if rms_result.outcome != RMSOutcome.PASS:
                raise ValueError("RMS must PASS before submitting a basket leg.")
            self._submitted_signals.add(duplicate_key)
```

Compare `submit_intent`, which does reject:

```77:80:backend/app/oms/oms_service.py
        duplicate_key = f"{intent.account_id}:{intent.signal_id}"
        if duplicate_key in self._submitted_signals:
            msg = f"Duplicate intent submission attempt for signal_id: {intent.signal_id}"
            logger.error(msg)
```

`BasketCoordinator.execute` submits exclusively through `submit_one_leg`
(`coordinator.py:266`), and `MAP.md` §Hop 16 confirms that is the production path. So
`OMSService._submitted_signals`, listed in `MAP.md` §5.4 as a live dedup barrier, is
write-only on every live order. The effective in-process guard is the adapter's map check:

```397:401:backend/app/oms/ibkr_adapter.py
        with self._lock:
            if order.internal_order_id in self._orders_by_internal_id:
                raise ValueError(
                    f"Duplicate order submission attempt for internal ID: {order.internal_order_id}"
                )
```

That one is sound: the check at `:397` and the registration at `:411-414` are separated by
only synchronous code (`_get_next_tws_order_id`, `_build_ibkr_contract`,
`_build_ibkr_order`) with no `await`, so no other loop task can interleave, and the TWS
thread never calls `submit_order`. Filed at P3 purely because the guard inventory
overstates the coverage.

---

## Critical sections guarded only by an in-process mechanism

Every entry is an `asyncio` primitive, a `threading` primitive, or a bare module-level
container. None is a database lock — `MAP.md` §5.1 confirms a repo-wide search for
`pg_advisory` returns nothing, and I re-confirmed it.

| # | Critical section | Mechanism | File:line | Sufficient under current topology? |
|---|---|---|---|---|
| 1 | Job execution serialised per `(account_scope, strategy_id)` | `asyncio.Lock` per key | `worker_pool.py:75`, held `:195` | **Yes.** Only the trading process claims jobs; ingest inserts only. Note `account_scope` is always `None` → `"default"`, so this is one lock per strategy across all accounts. |
| 2 | RMS read-modify-write on `symbol_exposures` / `model_value_used` / margin | `asyncio.Lock` per key, sorted acquisition | `order_manager.py:169`, `:963`, `:972` | **Partly.** Sufficient against loop concurrency; see C13 for the key-normalisation hole and C5 for the TWS-thread writer that ignores it entirely. |
| 3 | Adapter order maps, futures, exec-id sets | `threading.Lock` | `ibkr_adapter.py:92` | **Yes.** Correctly spans the loop/TWS-thread boundary and is held across all map mutations. |
| 4 | Gateway token buckets | `threading.Lock`, one shared instance | `gateway_rate_limiter.py:84`, instance at `main.py:46` | **Yes** for correctness of the buckets. See C9 for fairness. |
| 5 | Kill-switch armed-account set | bare module-level `set`, no lock | `kill_switch.py:45` | **Yes** as a cache — rehydrated from `kill_switch_operations` at `order_manager.py:229`, and only the trading process arms it. The defect is C3's stale read window, not the container. |
| 6 | Kill-switch operation uniqueness | **nothing** | `kill_switch.py:170-215` | **No.** Not even in-process — two concurrent handlers in one loop race. This is C1. |
| 7 | Single-pair close de-duplication | module `dict` of tasks | `position_close_service.py:33` | **No.** In-process only, and blind to the other two flatten producers. This is C2. |
| 8 | Broker-flatten de-duplication | module `dict` of tasks | `broker_flatten_service.py:34` | **No.** Same as above, keyed differently `(ibkr_account, con_id)` vs `(account_id, trade_id)`. |
| 9 | Basket-critical OPEN latch | `set` of `(account_id, strategy_id)` | `coordinator.py:79` | **Yes.** Backed by `baskets.state='CRITICAL'` and rehydrated at `coordinator.py:185`. |
| 10 | Retry-key suppression | `set` of strings | `coordinator.py:78` | **Yes** within one basket's lifetime; it never needs to outlive the coordinator. |
| 11 | Reconcile sweep exclusion | `asyncio.Lock` | `position_reconciler.py:266`, checked `:307` | **Yes.** One reconciler task exists. |
| 12 | Live-P&L persist bookkeeping | `threading.Lock` | `pnl.py:104` | **Partly.** Covers `_pending_pnl` / `_persist_in_flight`; does **not** cover `_next_req`, `_by_req`, `_marks`, `_legs`, which are touched from both threads. This is C8. |
| 13 | Account-margin snapshot dict | `threading.Lock` | `account_margin.py:182` | **Yes** inside the service. The lock is dropped before the listener fan-out at `:272`, which is where C5 happens. |
| 14 | Incoming-signals CSV | `threading.Lock` | `webhooks.py:32` | **No**, but harmless — it is a temporary debug artefact in the *ingest* process, so a second ingest worker would interleave rows. |
| 15 | Critical-recovery in-flight map | `dict` of tasks with done-callback | `critical_recovery.py:84`, `:116-137` | **Yes**, and this is the reference implementation the paths in rows 7–8 should follow. |

**Does the deployment topology make this sufficient?** For rows 1, 3, 4, 5, 9, 10, 11, 13
and 15: yes, and only because of a specific fact from `MAP.md` §1.1 — the trading
process is a **single** uvicorn process with a single event loop, launched once by
`deploy/systemd/trading-backend.service`. Every one of those guards fails silently the day
someone adds `--workers 2`, runs a second instance behind a load balancer, or splits the
worker pool into its own process. Rows 6, 7 and 8 are already insufficient today, inside
one process.

The one guard that would survive a topology change is `execution_claims`, and C2 shows it
is not on the flatten paths. The `signal_jobs` claim query would also survive, subject to
C4.

---

## Questions before treating these as defects

1. **C2 / flatten bypass.** Is skipping `_acquire_execution_claim` on the kill-switch,
   pair-close and reconcile-flatten paths a deliberate "the emergency button must never be
   refused by a stale barrier row" decision? If so, the finding narrows to "the three
   flatten producers do not exclude each other", which is a smaller fix.
2. **C3 / kill-switch scope.** The gate at `order_manager.py:748` is `intent.action == OrderAction.OPEN`
   only, so CLOSEs flow through an armed account. I have assumed that is deliberate
   (you want to be able to keep closing while halted) and have not filed it.
3. **C9 / flat priority band.** Was collapsing P1–P4 into one tier intentional, to stop
   diagnostics being starved indefinitely by order flow?
4. **Domain-lock granularity.** `account_scope` is never populated
   (`webhooks.py:250` omits it), so all accounts for one strategy share a single lock and
   execute strictly serially. Is the column reserved for future per-account partitioning,
   or is it meant to be populated today?
