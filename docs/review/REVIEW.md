# Consolidated review — IBKR pair-trading execution system

**Inputs (all present):** `MAP.md`, `BUGS-lifecycle.md`, `BUGS-concurrency.md`,
`BUGS-resilience.md`, `FINDINGS-hygiene.md`, `FINDINGS-docs.md`. Nothing was
missing.

**Method:** judgment over the six sessions, not a seventh hunt. Code was opened
only to adjudicate contradictions before promoting or dropping a claim. Read at
the same 2026-09-03 tree `MAP.md` describes. Paths are relative to
`/home/tradingapp/app`.

**What this is not.** A re-score to make the list shorter. Several P0s are the
same defect seen twice; those are merged. Several P0s from hygiene are the same
code fact with an unconfirmed trigger; those are kept and the severity
disagreement is stated. Nothing was softened to make the report look better.

---

## 1. Deduplicated master table

Source IDs: `L` = lifecycle, `C` = concurrency, `R` = resilience, `H` =
hygiene, `D` = docs-audit code defect (`FINDINGS-docs.md` §3). A row that lists
two source IDs is one defect, not two opinions of similar bugs.

Where sessions disagree, the **Adjudication** column is the decision and why.
Blank means they agreed on the facts and the severity.

| ID | Sev | Conf | Sources | Location | Defect | Observable | Adjudication |
|---|---|---|---|---|---|---|---|
| **M1** | **P0** | certain | R1 | `oms/coordinator.py:266-275` → `ibkr_adapter.py:438`; recovery at `execution_claim_repository.py:186-201` | `placeOrder` returns before the `orders` row commits. Every recovery path asks `count_orders_emitted` and treats zero rows as "never reached the broker", then `release`s the claim to `ABANDONED` (the only state `ON CONFLICT` may retake) and requeues the job | Two IB order IDs for one `trade_id`; `Released stale execution claim` then `Recovery requeued job_id=…`; doubled position | — |
| **M2** | **P0** | certain | C2, R2 | `kill_switch.py:395-418`; `position_close_service.py:138-180`; `broker_flatten_service.py:149-183` | The three flatten producers do not exclude each other. Each mints a fresh `uuid4()` into `signal_id`, so no unique index can relate two attempts at the same close. In-process dicts (`_IN_FLIGHT_PAIR_CLOSES`, `_IN_FLIGHT_BROKER_FLATTENS`) are private, keyed differently, gone on restart, and the kill-switch flatten has no such dict at all. Skipping `_acquire_execution_claim` is **intentional** (emergency button must never be refused by a stale barrier). The remaining hole is mutual exclusion + a stable identity so a second click can be answered "already flattening" or retaken only after a broker snapshot says the position is still live | Duplicate `KILLSWITCH-` / `CLOSEPAIR-` / `RECON-FLAT-` baskets; position flips from flat to net opposite at IB | **Author: bypass is deliberate.** Narrowed from "put `execution_claims` on flatten paths" to "the three producers must exclude each other; any flatten lock is retakeable after a broker snapshot, not after `count_orders_emitted == 0`". Concurrent-click and click-after-restart remain the same P0 |
| **M3** | **P0** | certain | C1 | `kill_switch.py:170-218`; `db/models/kill_switch.py:27-38` | `initiate_square_off` is SELECT-then-INSERT. The "strict idempotency" SELECT only matches `ACTIVATING`/`FLATTENING`/`RECONCILING`/`RETRYING` — **not** `COMPLETE`/`UNRESOLVED` — so a second square-off after flatten finishes inserts a new row and starts a second flatten. No unique/partial unique on `(account_id)` for armed statuses, so two in-flight POSTs both get `created_new=True` | Two `KILL_SWITCH_ACTIVATED` events; account goes long→short by the original size; or a second flatten after `COMPLETE`/`UNRESOLVED` | Distinct from M2. **Author: second square-off while armed is refuse** — including `COMPLETE` and `UNRESOLVED`. They must Start Again (`CLEARED`) first. Unique predicate = `_ARMED_STATUSES` (everything except `CLEARED`). Today's SELECT is too narrow |
| **M4** | **P0** | certain | L3 | `kill_switch.py:483-499` | After a one-leg-filled flatten, compensation *re-buys* the filled leg. The reconciler counts every `KILLSWITCH-` / `:CLOSE` `FILLED` row and does not exclude `is_compensation`, so `len(filled_close) >= req_legs` and `close_trade` marks the pair `CLOSED`. Exit marks collide on the filled symbol, so realised P&L is computed from the compensation price | `positions.risk_state='CLOSED'`, `kill_switch_operations.status='COMPLETE'`, IB still holding both original legs; `realised_pnl` from one leg at the unwind price. Reproduced: `test_kill_switch_does_not_count_compensation_order_as_close_fill` | — |
| **M5** | **P0** | certain (defect) / A1 (trigger) | L1 | `ibkr_adapter.py:786`, mutation `:814-831` (no terminal guard; contrast `:585`) | `execDetails` after a terminal `orderStatus` (typically `Cancelled` from `coordinator.py:409`) writes the fill, regresses `CANCELLED` → `PARTIALLY_FILLED`, and is never compensated — `_compensate_filled` already sampled `filled_quantity` | `orders.status='CANCELLED'` with `fill_qty > 0`, no `is_compensation` sibling; IB holds a naked leg. Reproduced: `test_exec_details_after_cancel_does_not_regress_terminal_order` | — |
| **M6** | **P0** | likely / A2 | L2 | `ibkr_adapter.py:606-607` interacting with `:585`; `openOrder` at `:768` passes `qty_filled=None` | A status string that maps to `FILLED` with no quantity makes the order terminal at `filled_quantity==0`. Later real quantities are discarded. `_basket_complete` sees 0, compensates nothing, `_compensation_complete([])` is `True` (`coordinator.py:594-595`) → basket `COMPENSATED` | `orders.status='FILLED'`, `fill_qty=0`; `baskets` `COMPENSATED` with zero compensation; `BROKER_ORPHAN`; live IB position, no `positions` row. Reproduced: `test_filled_status_without_quantity_does_not_block_real_fill` | — |
| **M7** | **P0** | certain (sticky persist + compensate-on-ERROR + no reconnect) / likely (latch clear) | L14, R8 | `ibkr_adapter.py:1017-1030`; persist stickiness `order_repository.py:13,108-115`; leftovers `critical_recovery.py:296-300,231-240` | **Author: mark `ERROR` while the socket is down; auto-reconcile when it is back.** The mark itself is intended (A4, for disconnect). The defects are everything around it: (1) no reconnect loop exists, so "when it is back" never runs; (2) `_notify_future_if_terminal` makes the coordinator's wait return immediately, then `_compensation_complete([])` is True → `COMPENSATED` while IB still holds the orders; (3) `_persist_child` writes `ERROR`, which is in `_TERMINAL_ORDER_STATUSES` and will not be overwritten by a later real fill — so even a future reconnect cannot book the fill. If the basket was already `CRITICAL`, recovery reads `fill_qty=0` and may clear the OPEN latch | `Connection closed unexpectedly` → `orders.status='ERROR'` with `fill_qty=0`; `BASKET_COMPENSATED` or `BASKET_CRITICAL_CLEARED … No filled non-compensation legs`; IB shows a filled position the ledger denies | **A4 settled for the mark, not for the aftermath.** Lifecycle P2 rejected for sticky persist + empty-compensate. Do **not** "fix" by stopping the ERROR mark. R10 (pacing-timeout on *cancel* → ERROR) is **not** covered by this decision — still a sibling defect, keep with M7 until confirmed |
| **M8** | **P0** | certain | D§3.1, D#2 | `api/routes/webhooks.py:147-160`; defaults `core/config.py:110-111` | `webhook_auth_enabled=True` and `webhook_auth_secret=None` (shipped defaults) take the `if expected_secret:` false branch: no header check, no warning. Sibling `emergency.py:40-47` fails closed on the same shape | Any POST to `:8000/api/webhooks/tradingview` is accepted. ngrok is the production ingest path | Promoted from the doc audit. It is a code defect, not drift |
| **M9** | **P0** | certain / A3 | L4 | `model_blue/persistence.py:64-70`; `submitted.extend` at `coordinator.py:360` | A 2-leg OPEN that completes via auto-square-off retry has 3 non-compensation child orders. `_open_trade_from_fills` raises `POSITION_REQUIRES_FILLS`. `ModelBlueValidationError` is swallowed as an account rejection (`order_manager.py:772-787`). Basket is `OPEN`, claim already sealed, in-memory exposure already booked, **no `positions` row**. Later CLOSE hits `NO_OPEN_POSITION` forever | `baskets.state='OPEN'` with FILLED orders and no `positions` row; `signals.status='REJECTED'` / `POSITION_REQUIRES_FILLS`; every CLOSE for that `trade_id` rejected | **Author: remainder-retry is intended on live (4001).** Dormant today only because `paper_retry_ports_allowed` excludes 4001. That gate is a blocker, not a safety policy. **Do not lift it until this persist crash is fixed.** Back in the live-money sequence |
| **M10** | **P1** | certain | L5 | `order_manager.py:1288-1293` vs `:1305-1310`; notional at `:1200-1207` | Exposure is booked at entry fill prices and released at exit fill prices (`leg.effective_notional` of `filled_intent`). Price up → release exceeds booking, `max(Decimal(0), …)` at `:1308` eats other positions' exposure. Price down → residue stranded until restart | `MONEY_LIMIT_EXCEEDED` whose `existing` matches no open `positions` row; or `symbol_exposures` at 0 with positions open | **Author: keep the floor as a safety net.** Do not raise on negative. The defect is still the entry/exit mismatch (or, better, rebuild from `positions`). After that fix, `max(Decimal(0), …)` stays so a remaining leak cannot store a negative bucket and poison check 8. If the floor trips, log it — that is leftover drift, not a valid write |
| **M11** | **P1** | certain | L6 | `kill_switch.py:429-462`; `position_close_service.py:200-236` | Both write `positions` / `event_log` and never touch `RMSContext`. `open_positions`, `symbol_exposures`, `model_value_used`, `margin_commitments` keep counting the closed pair. `LivePnlService.unwatch` is never called | `OPEN_POSITION_LIMIT_REACHED` where the DB has fewer; tick recomputes for a closed `trade_id` until restart | — |
| **M12** | **P1** | certain | L7 | `order_manager.py:1100`, `:1115` → `_record_unsettled_exposure` `:1210-1250` | `COMPENSATED` is not in `(OPEN, CLOSED)`, so exposure is booked from the *submitted* fills as if risk remained. For a fully-unwound OPEN that is the opposite of the truth; for an unwound CLOSE (compensation restored the position) exposure is subtracted for a still-open pair. The docstring at `:1213-1224` justifies this by "real risk sat at the broker" — false for `COMPENSATED` | `UNSETTLED_EXPOSURE_BOOKED` immediately after `BASKET_COMPENSATED`; `symbol_exposures` non-zero with no open positions | — |
| **M13** | **P1** | certain | L8, L11, H19 | `position_repository.py:220-223`; `persistence.py:28-38,44-57,216-218`; dead readers `execution_repository.py:14,30,39,55,63` | Realised P&L never sees entry commission (only the close basket; `persist_open` writes none). `commissionReport` arriving after `after_submit` makes `_commission_from_orders` return `None` (the `if execs: continue` at `:51` skips the `order.commission` fallback). The durable `executions` ledger is write-only for pricing: `weighted_average_price` / `list_by_internal_order_id` have no production caller | `positions.commission` ≈ half of `sum(executions.commission)` for the trade; later-arriving commissions never correct it; a restart between fill and persist falls back to the broker aggregate | Three faces of one authority failure: the fill ledger is not the P&L authority the design claims |
| **M14** | **P1** | certain / A3 | L9, L22, L23 | `persistence.py:37`; `coordinator.py:865`; `order_repository.py:140-156`; `order_manager.py:1186-1207` | Auto-square-off retry mints `signal_id=…:RETRY:L{i}:{n}`. Nothing strips `:RETRY:` (`:UNWIND:` is stripped at `coordinator.py:1040`). The callback persist wins the insert; `trade_id` is insert-only so the later original-intent persist cannot correct it. Exit marks overwrite per symbol (`marks[order.symbol] = raw`). `_intent_with_fills` sums qty across clips then takes the *first* price | `orders.trade_id` like `MBG-…:RETRY:L0:1` beside the bare id; extra `signals` row; kill-switch `list_by_trade_id` under-counts; `realised_pnl` not reproducible from `executions`; RMS notional = `retry_qty × (first_price − retry_price)` off | **Author: same trade.** Strip `:RETRY:` like `:UNWIND:` on persist (`coordinator.py:1040` and `_ensure_signal_pk`). Identity face is a confirmed bug, not an audit choice. Quantity-weight still required. Live once remainder-retry is on |
| **M15** | **P1** | certain | L10 | `position_repository.py:205-218`; worst caller `kill_switch.py:490-499` | A missing `exit_marks` entry (symbol collision as in M4, or `fill_price IS NULL`) contributes `0` to realised P&L. The row is still `CLOSED`. No "incomplete marks" path | `realised_pnl` equal to exactly one leg on a two-leg pair | — |
| **M16** | **P1** | certain | L12 | No reaper. Sweeps exist for `signal_jobs`, `baskets`, `execution_claims` only | Broker-silent working orders sit `PENDING`/`SUBMITTED`/`PARTIALLY_FILLED` forever. `recover_incomplete_baskets` leaves a no-fill basket in `EXECUTING` (`coordinator.py:507-515`). `fetch_in_flight_accounts` then stamps every reconcile diff `in_flight=true` | Non-terminal `orders` older than the session; `baskets.state='EXECUTING'` from previous days; operator dismisses every mismatch as in-flight | — |
| **M17** | **P1** | certain | L13, R6 | `recovery.py:66-73`; `ibkr_adapter.py:453-461` (`adopt_order`) has no application callers; callbacks `:756-762`, `:796-798` | `fetch_broker_order_snapshot` fires `reqOpenOrders` / `reqExecutions`, but `_orders_by_tws_id` is empty after restart, so every replayed callback is ignored. The comment "warms adapter state" is false | `Requested open orders / executions snapshot` then `Ignoring openOrder for unknown tws_id=…`; fills during the outage never appear in `orders`; reconciler `BROKER_ORPHAN`, no action | Same defect, two sessions. Merged |
| **M18** | **P1** | likely | C4 | `signal_repository.py:366-375`, `:387-390`; lock is `of=SignalJobModel` only | Sibling `EXISTS` reads uncommitted-invisible `QUEUED` rows. Two workers in one poll tick claim OPEN and CLOSE of the same `trade_id`. Domain lock then orders them arbitrarily. CLOSE-first → `NO_OPEN_POSITION` → job `REJECTED` (terminal). Idempotency key refuses a resend | Position stuck open; `REJECTED` job with `NO_OPEN_POSITION`; only a manual close clears it | Not reproduced against Postgres. **Author: ingest must populate `account_scope` today** — that partitions *different* accounts; same-trade OPEN/CLOSE still share a lock. M18 remains a claim-query bug. Populate does not replace the `trade_id` sibling fix |
| **M19** | **P1** | certain | C5 | `order_manager.py:376-389` ← `account_margin.py:272-274` (TWS thread); loop write `:404-406` | TWS-thread snapshot listener replaces `margin_commitments[acct]` between the loop's `setdefault` and `append`. The `__margin__` lock is `asyncio.Lock` and invisible to that thread. Same gate also runs *outside* `_exposure_guard` (`:745` vs `:1016`) | A just-committed OPEN is invisible to the free-funds gate; next OPEN admitted over headroom | — |
| **M20** | **P1** | certain | C6, H16 | `pnl.py:586-590` → blocking `request_contract_details` (`tws_client.py:410-433`); caller `order_manager.py:1109` inside the exposure guard | Up to ~6 s `time.sleep` + `Event.wait` per leg, ~12 s per pair, on the event loop, holding symbol / margin / model-value locks. The async wrapper exists (`tws_client.py:445-449`) and CFD discovery uses it | Spurious `BASKET_UNWINDING` on baskets whose legs filled (timeouts expire against wall clock while the resolving callback sat in the ready queue); heartbeat latency bursts on every OPEN, and again at startup hydrate | Same defect. Merged |
| **M21** | **P1** | certain | C3, R12 | Gate at `order_manager.py:748`; submit at `:1081`; never re-read in `coordinator.py:195,663,871` | Kill-switch is read once per account, then ~6 awaits (instrument resolve, optional what-if, claim, persist, pacing — up to ~15 s) before `placeOrder`. Flatten enumerates `positions` (the in-flight OPEN has none yet), reports `COMPLETE`, then the OPEN submits. Retry / compensate after arming also never re-check | New open position on an account whose kill-switch endpoint says active and whose operation is `COMPLETE`; or `KILL_SWITCH_ACTIVATED` followed by `AUTO_SQUARE_OFF_RETRY` | Two faces of one "gate is advisory past step 1". Merged |
| **M22** | **P1** | certain (no resume) | C7, R11 | `kill_switch.py:297-302`; no reaper for `kill_switch_operations` | Flatten is `asyncio.create_task` with no stored reference and no startup resume. A crash (or an unretrieved exception) leaves `FLATTENING` forever; the account stays armed (`_ARMED_STATUSES` includes `FLATTENING` and `COMPLETE`) and not flat | `kill_switch_operations` stuck `ACTIVATING`/`FLATTENING` with `flattened_count=0`; or a `FLATTENING` row whose `updated_at` predates the process | **Partial drop of C7.** The "task is GC-eligible mid-flight" claim is overstated on CPython 3.11+ (the loop holds a strong ref to scheduled tasks). The unretrieved-exception and no-durable-resume faces stand. Merged under R11's facts |
| **M23** | **P1** | certain | R3, R18 | Claim query `signal_repository.py:380-386` vs reclaimer docstring `:488-492` and dead-letter arm `:496-511` | Expired-lease `PROCESSING` is claimable by a 0.5 s worker poll; the 15 s reclaimer (which sleeps before its first sweep) loses the race, so the "quarantine, do not re-execute" policy is advisory. Separately, the dead-letter arm matches `PROCESSING` and does **not** call `count_orders_emitted` — a job that reached the broker is filed terminal | `Worker X LOST its lease on job Y`; `DEAD_LETTER` with live `orders` rows; `Stale lease sweep: … dead_lettered=1` | Two arms of one policy contradiction. Merged |
| **M24** | **P1** | certain | R4 | `order_manager.py:210-219`; callers `main.py:85` and `recovery.py:148-149` | `hydrate_runtime_from_db` accumulates. Called twice on any restart that has pending jobs or baskets — exactly the restart where limits must be right. `processed_signals` is a set (safe); `model_value_used` self-heals on the 30 s reconcile; `open_positions` and `symbol_exposures` stay doubled for the process lifetime | `Hydrated runtime from DB` twice; `OPEN_POSITION_LIMIT` / `MONEY_PER_STOCK` rejecting valid signals | — |
| **M25** | **P1** | certain (ordering) / likely (fires) | R5 | `order_manager.py:224-229`; swallow `main.py:84-87` | `hydrate_kill_switch_cache` sits *after* `hydrate_critical_from_db` / `recover_incomplete_baskets` in the same `try`. Any raise there leaves the armed-account cache empty | `Failed to hydrate Model Blue/RMS runtime state` with no following `KILL SWITCH REARMED FROM DB`; new OPENs on an account whose `kill_switch_operations` row is still armed | — |
| **M26** | **P1** | certain (deferral) / likely (escalation skipped) | R7 | `main.py:85` before TWS connect `:105`; only connected pass is `recovery.py:148-149`; `coordinator.py:482,488-496` | First `recover_incomplete_baskets` always logs `BASKET_RECOVER_DEFER`. The only pass that can escalate is at the tail of `run_startup_recovery`. Any exception before that line leaves `EXECUTING` with no CRITICAL latch | `BASKET_RECOVER_DEFER` with no later `BASKET_RECOVER_CRITICAL`; naked leg at IB; new OPENs accepted | — |
| **M27** | **P1** | certain (code) / likely (trigger) | C13, H1, H2, H3, H4 | Parser `model_blue/parser.py:140`; lock `order_manager.py:987-991`; check 8 `money_per_stock.py:56,68`; hydrate `:611,:618` | Leg symbol is `.strip()`ed and never case-folded, while `action` in the same function is `.upper()`. Exposure locks, `symbol_exposures`, and `per_symbol_limits` (written UPPER at `config_service.py:407`) all key on the raw string. `"AAPL"` and `"aapl"` take different locks and different budget buckets; a lowercase inbound misses the configured limit and falls through to the strategy default | Aggregate exposure on one symbol exceeding its limit, two `symbol_exposures` buckets differing only in case | **Disagreement, resolved.** Hygiene filed H1/H2/H3 as P0/certain. Concurrency filed C13 as P2/likely. The code defect is certain. The money-loss path requires TradingView to emit inconsistent casing, which no session confirmed against live alert templates. That is not a P0 under normal consistent templates. **P1.** Facts stand |
| **M28** | **P1** | certain | D§3.2, D#11 | `order_manager.py:150`, `:1448`; check 8 `money_per_stock.py:59-66` | `abd604e` made check 8 fail-closed and removed the `$10M` ORM default from `accounts.default_symbol_limit`. `_ensure_strategy_config` still plants `money_limit_per_symbol=Decimal(10_000_000)` on every account before `build_intent`, and never copies a DB money limit onto that field. `strategy_limit` is therefore never `None` on the live path; `NO_SYMBOL_LIMIT_CONFIGURED` is dead; an unconfigured symbol is capped at $10M | Every OPEN on a symbol with no per-symbol / default-symbol row is silently permitted up to $10M | Promoted from the doc audit. Check 7 does *not* have this hole |
| **M29** | **P1** | certain | H5 | `rms/models.py:102-106`; `rms/checks/duplicate.py:27-29`; writer `order_manager.py:1283` | Duplicate barrier keys on un-normalized `strategy_id` / `signal_id`, and check 2 reimplements the helper it already imports around rather than calling `duplicate_lookup_key` | A re-sent alert whose `strategy_id` differs only in case is not a duplicate. Two copies of the barrier that agree today by coincidence | — |
| **M30** | **P2** | likely | C8 | `pnl.py:638-639` vs `:435-436` | `_next_req += 1` from the loop *and* the TWS-thread reroute handler. The service lock at `:104` covers persist bookkeeping only | Ticks for one symbol booked as another's mark; `/demo/market-data-health` `last_mark` mismatches the symbol | — |
| **M31** | **P1** | certain (dead knob + collapse vs intent) / likely (fires at open) | C9, D§3.3 | `gateway_rate_limiter.py:259-274`; unused `emergency_reserve` `:57,:69-70,:78` | P1–P4 are indistinguishable; waiters poll rather than queue. `IBKR_GATEWAY_EMERGENCY_RESERVE_PER_SEC` is validated, stored, and never read — the reserve is implicit `max − normal` | `BASKET_UNWINDING` / `Gateway pacing timeout` on leg 2 at the open, when market-data resubscribe drains the normal bucket | **Author: orders first.** Collapse is **not** intended. P0 flatten stays on top; P1 must queue ahead of P3/P4. Honour `emergency_reserve` (`normal = max − reserve`) or delete the setting. Bumped P2 → P1 |
| **M32** | **P2** | likely | C10 | `coordinator.py:1146-1152`; pool `db/session.py:33-36` | Every `orderStatus` / `execDetails` / `commissionReport` schedules an unbounded persist coroutine, each with its own session. Pool 20+30, `pool_timeout=30` = the job lease | Heartbeat warnings during fill bursts, then `Stale lease sweep` for jobs whose workers are still running. Feeds M33 | — |
| **M33** | **P2** | certain | C11 | `worker_pool.py:252-255` | Heartbeat `except Exception` logs and loops; it does not set `lease_lost` (the `not renewed` arm at `:244` does). Worker continues placing orders on a lease the reclaimer already took; fence then blocks the terminal write | `RECOVERY_REQUIRED` whose `last_error` says "broker state unverified" but whose orders are `FILLED` and whose positions booked. Indistinguishable from a genuine mid-execution death | — |
| **M34** | **P1** | certain (no reconnect + None→1) / speculative (concurrent increment) | R9, C12 | `tws_client.py:151-156,76-87`; `ibkr_adapter.py:203-209`; log lie `main.py:112` | No reconnect loop — and reconnect is now the intended recovery for M7. Socket drop sets `next_order_id = None`. A submit that passed `is_connected()` then hits `_get_next_tws_order_id` falls into `if current_id is None: current_id = 1`. Separately, `nextValidId` writes the counter under no lock while the adapter increments it under `_lock` | Log claims "execution adapter will auto-reconnect on active traffic" (there is no such code); or an IB duplicate-order-id rejection on ID 1 | **Author: auto-reconcile when the socket is back.** Missing reconnect is no longer a latent ops gap; it is the unimplemented half of M7. Bumped P2 → P1. C12 lock is a precondition of that reconnect, not a reason to skip it |
| **M35** | **P2** | certain | R13 | `order_manager.py:186` `fill_timeout=90`; systemd `TimeoutStopSec=15`; `process_manager.py:120,674` | `worker_pool.stop()` cancels immediately. `has_in_flight_jobs()` is never consulted at shutdown (only `MarginScanner` reads it). Grace is 15 s (or 5 s on `_ensure_fastapi`) against a 90 s fill wait | `Restarting FastAPI so it can handshake with the new Gateway session` mid-session, then SIGKILL; `PROCESSING` jobs with a live lease | **Author: wait for in-flight baskets.** Not a deliberate "kill it and reconcile later". Drain via `has_in_flight_jobs()`; `TimeoutStopSec` must exceed `fill_timeout` (and any reconnect wait) |
| **M36** | **P2** | certain | R16 | `pnl.py:468-469`; resubscribe only at `:282-285` | `on_connection_closed` is `return`. `_resubscribe_all_active` runs on IBKR error 1101, which requires a socket that recovers — and nothing reconnects the socket (M34) | Dashboard P&L frozen; `active_subscriptions` still non-zero | — |
| **M37** | **P2** | certain | R17 | `coordinator.py:1146-1148,203` | `self._loop` is set only inside `execute`. A broker callback before the first basket of the process lifetime is dropped with no log | Silent fill loss at startup, before any signal has run | — |
| **M38** | **P2** | certain | R19 | `recovery.py:41-45,93,132-136`; `signal.py:60` | A job quarantined `RECOVERY_REQUIRED` is requeued to `QUEUED` on the next startup whenever `count_orders_emitted` is 0 — which is exactly the M1 window | `Recovery requeued job_id=… (no orders emitted)` for a job that was quarantined as "broker state unverified" | Downstream of M1. Fixing M1 makes this mostly inert; until then it is a second trip through the same hole |
| **M39** | **P2** | certain | L16 | `instruments/resolver.py:398-418`; `rms/models.py:70-74` | `quantity` is rounded; `notional` is not. `effective_notional` prefers the stored (pre-round) notional. RMS is conservative; `_commit_margin` and `INSTRUMENT_RESOLVED` audit a number that does not match the submitted quantity | `event_log` `INSTRUMENT_RESOLVED` where `quantity × price ≠` the `margin_impact` on the matching `orders` row | — |
| **M40** | **P2** | certain (no cross-check) / speculative (A9 trigger) | L24 | `position_repository.py:207-218`; close sizing `position_close_service.py:170` then `_resolve_instruments` | Realised P&L uses the *open* signed qty. Nothing compares it to the close basket's filled qty. A `size_increment > 1` can round a close down while the row is closed at full size | `risk_state='CLOSED'` on full qty; `broker_positions` still carries a line; perpetual `BROKER_ORPHAN` | — |
| **M41** | **P2** | certain (hole vs intent) | L18 | `position_repository.py:175-191`; gate `strategy.py:157-160` only calls `get_open` | Re-opening a previously-closed `trade_id` mutates the row back to `OPEN` and leaves `realised_pnl` / `commission` / `live_pnl` holding the previous cycle. Next `close_trade` destroys that history. `_build_open_intent` only refuses while the row is still OPEN | Freshly opened pair already showing non-zero `realised_pnl` on `/demo/positions` | **Author: unique forever** (per account). Reuse is a TradingView bug. Do **not** version rows. Duplicate check 2 refuse is intended after CLOSE too. Remaining defect: persist_open / `_build_open_intent` must refuse if **any** `positions` row exists, not only OPEN. RMS `processed_signals` must hydrate historical ids so restart cannot reopen |
| **M42** | **P2** | certain | H6, H7 | `position_reconciler.py:354,141`; `critical_recovery.py:348`; `reconcile_service.py:67,72`; `api/routes/reconcile.py:43,68` | Broker→internal account map is built and probed raw, on the same lines that `_norm_symbol` the adjacent fields. Reconcile authorization compares raw; `config.py:64` compares normalized | Real broker positions classified `MISMATCH_UNMAPPED_ACCOUNT`; authorize/scope succeeds or fails on capitalization | — |
| **M43** | **P2** | certain | H8 | `api/routes/orders.py:30,50,72` | Three handlers filter on `OMSOrder.account`, which does not exist. Account lives on `order.intent.ibkr_account` (`ibkr_adapter.py:241`) | Any `role == "user"` caller of list / get / cancel raises `AttributeError` → 500. Admins skip the branch | — |
| **M44** | **P2** | certain | H11 | `tws_client.py:137-149` and 14 `except AttributeError: pass` sites including `:365` (`on_exec_details`) and `:330` (`on_order_status`) | The swallow cannot distinguish "listener lacks the method" from "AttributeError *inside* the method". On the fill and terminal-status paths that drops a callback with no log | A fill or a terminal status vanishes; no line in the log | **Verified, not a guess.** `_listeners` mixes `IBKRExecutionAdapter` (has fill methods), `AccountMarginService` (account only), and `PositionSnapshotCollector` (positions only). Skipping a missing method is **load-bearing**. The *form* (`except AttributeError: pass`) is copy-paste from the same boilerplate (`6403eb3` orderStatus, `902ca4d2` execDetails). Market-data already uses `hasattr`. Author: accidental. Fix: `hasattr` before call so an internal `AttributeError` in the adapter logs. Do not delete the skip |
| **M45** | **P2** | certain | H15 | `api/routes/service_control.py:39,55,63,82` | `async def` handler calls blocking `subprocess.run` (timeouts 5 / 5 / 15 s) with no `asyncio.to_thread` | Admin restart of `ibgateway` from the UI freezes fill handling for up to 15 s in the process that also runs the worker pool | — |
| **M46** | **P2** | certain | D§3.4, D#38 | `core/config.py:46` (7497); `process_manager.py:127` (4002); `watchdog/config.py:42` (4001) | Three components, three default ports. **Author: live Gateway 4001 is the only port.** Watchdog already defaults 4001. `Settings.ibkr_port` still defaults 7497 (paper TWS); `process_manager` still waits on 4002 (paper Gateway) — that unit is disabled (Q4), so the remaining live defect is the Settings / `.env.example` default | A host that omits `IBKR_PORT` connects the app to paper TWS 7497 while the watchdog probes live Gateway 4001 | **Author: 4001.** One setting, three readers all 4001. Remainder-retry on 4001 is a separate gate (`paper_retry_ports_allowed`); do not confuse port-default fix with turning retry on |
| **M47** | **P2** | certain | H17 | `api/routes/config.py:202,251,312`; `reconcile.py:76`; `emergency.py:116` | Five handlers open a second `AsyncSessionLocal` while already holding the request-scoped session | Two independent transactions per logical request; a partially-applied operation is possible where the code reads as atomic | — |
| **M48** | **P2** | certain (naming / persist mismatch) | H39 | `core/config.py:104-107`; `instruments/execution_override.py:1-8` | TradingView / Model Blue send `STK`; submit maps to IBKR **CFD**. **Author: CFD is the intended production instrument for Model Blue; options will be added later.** Default `True` is correct — do not turn it off. Remaining defect: the flag and module are labelled `TEMPORARY` / `paper_` / `_DEMO` / `PAPER_EXECUTE_STK_AS_CFD`, and the persisted signal still says STK. An operator who "disables paper" places live Model Blue as STK | Incident response treats ledger STK as the contract; `PAPER_EXECUTE_STK_AS_CFD=false` silently changes secType on live money | **Author: mapping stays.** Rename to production language; keep the resolve-time map (do not copy into the adapter). Persist or log executed secType. Future OPT is a new requested type, not a reason to delete this map |
| **M49** | **P2** | speculative | R14 | `gateway_rate_limiter.py:82-90` | Restart initialises both buckets full and clears Error 100 cooldown. A crash-loop can issue a 30-message burst per restart | Error 100 shortly after a restart-and-resubscribe storm. Narrow: `RestartSec=10` plus a fresh IB socket | — |
| **M50** | **P2** | certain (files) / likely (live race until host is checked) | R20 | `deploy/systemd/process-manager.service` + `trading-backend.service` (both `WantedBy=multi-user.target`) | Both can launch the identical uvicorn on `:8001`. `process_manager._ensure_fastapi` also SIGKILLs on a 5 s grace (M35) | `EADDRINUSE` in `fastapi.log`; supervisor restart-budget exhaustion | **Author: systemd units only.** `process-manager.service` must not be enabled. Watchdog stays observe/notify (it already does not `systemctl`). M50 is a misconfig if `process-manager` is enabled, not an open design question. Confirm with `systemctl is-enabled` |
| **M51** | **P3** | speculative / A5 | L15 | `ibkr_adapter.py:54-63,892` | `_usable_price` rejects `<= 0`, so an exchange rebate becomes `0`; even if it survived, `commission > 0` would skip it | `executions.commission = 0` on rebated fills | — |
| **M52** | **P3** | speculative / A7 | L20 | `ibkr_adapter.py:807-809` | Empty `execId` synthesises `noid:{iid}:{shares}:{price}:{cum_qty}`; two identical fills with absent `cumQty` collide and the second is dropped | `executions` count short of the broker's fill count by one clip | — |
| **M53** | **P3** | speculative / A8 | L25 | `oms/models.py:109-115`; `execution_repository.py:30-36` | Commission totals ignore `commission_currency`. No FX conversion exists under `backend/app/` | `positions.commission` as a bare sum of mixed-currency rows | Dormant until a non-USD leg appears |
| **M54** | **P3** | certain | D§3.5, D#22, H del. #3 | `webhooks.py:59-63` (no callers); `scripts/prune_webhook_captures.py:46`; live CSV `webhooks.py:127-141` | JSON capture is dead; the documented pruner globs `webhook_*.json` and is a permanent no-op; the file that actually grows (`incoming_signals.csv`) is unmarked and unpruned | Operators hunt `backend/data/tradingview_webhooks/*.json` during an incident and find nothing; payload is in `signal_jobs.capture_data` | **Author: delete** `_save_raw_capture_file`. Authority is `signal_jobs.capture_data`. Fix docs + pruner. CSV is a separate TEMPORARY path (H41), not in this delete |
| **M55** | **P1** | certain | H23 | `api/deps.py:67-75`; `demo_streaming/api.py:69-77`; guard `core/config.py:142-148` | `TRADINGAPP_TESTING=1` promotes any unauthenticated request to synthetic admin. Settings only refuse the flag when the DB name is `ibkr_trading`. A trading process on any other name (or a misspelled prod URL) is open admin with `placeOrder` | Unauthenticated `POST .../square-off` / `service-control` on `:8001` | **Author: never on a process that can place orders.** pytest/`conftest.py` is the only legitimate setter. DB-name guard is not enough. Startup must refuse the flag in `trading-backend` (and any other `placeOrder` process). Grep systemd `EnvironmentFile`s |

Hygienic P3s that are real inconsistencies but not money-path defects (H9 abandoned `core/identifiers.py`, H10 duplicated `_norm_symbol`, H12/H30 acquire-loop duplication, H13 six retry policies, H18 commit-by-hand, H22 dual pacing constants, H24/H25 magic numbers, H26/H27 naming, H28 `trade_id` surgery in PnL, H31–H37, H40, H41, H43) stay out of this table. **H23** promoted to **M55**. **H42** (paper-port retry allowlist) is the gate in step 9 / 26b. They are listed under the identifier-normalization and "two sources of truth" clusters in §2 and the dropped/bloat notes in §7.

---

## 2. Root causes

The table in §1 is a symptom list. These eight clusters are why the same shapes keep recurring. Fix the cluster, not the row, or the next session will file the same bug under a new ID.

### RC1 — Recovery asks "did we persist?" not "did we submit?"

The system's entire crash story is one predicate, `count_orders_emitted`
(`execution_claim_repository.py:151-167`), evaluated against a row that is
written *after* `placeOrder` (`coordinator.py:266-275`). Four independent
callers — `reconcile_stale_claims`, `RecoveryManager.run_startup_recovery`,
`OrderManager._resolve_failed_claim`, the worker failure handler — all get the
wrong answer in the same window.

Members: **M1**, **M23** (dead-letter arm uses no ledger check at all), **M38**
(quarantine silently undone by the same predicate), **M17** (cannot even ask
the broker, because `adopt_order` is unwired).

Until this predicate is inverted — "a live `CLAIMED` claim *is* evidence of a
possible submit" — every other recovery fix is building on a lie.

### RC2 — Flatten producers have no shared exclusion (webhook barrier is intentionally absent there)

`execution_claims` is described, correctly, as "the authoritative barrier"
(`db/models/execution_claim.py:1-8`) **on the webhook path**. It is acquired in
one place: `order_manager.py:1075-1077`. Three other producers reach
`placeOrder` without it. **Author decision: that bypass is deliberate** — the
emergency button must never be refused by a stale barrier row. Do not "fix" M2
by calling `_acquire_execution_claim` on flatten.

What remains a defect: the three flatten producers do not exclude *each
other*, each mints a `uuid4()` so two attempts cannot be related, and
kill-switch idempotency is a check-then-act on a table with no unique
constraint, so even one process can arm twice. Any replacement lock must be
retakeable after a broker snapshot says the position is still live — not after
`count_orders_emitted == 0` (that predicate is M1's lie).

Members: **M2** (mutual exclusion + identity, not the missing claim), **M3**,
**M4** (the compensation row is a fourth identity the reconciler cannot
distinguish), **M14** (`:RETRY:` is a fifth — live once remainder-retry is
on; author: same parent `trade_id`).

### RC3 — Local order status is treated as broker truth, and terminal is not terminal

Five writers of `OMSOrder.status`; only `_apply_mapped_status` respects the
terminal set (`ibkr_adapter.py:575-611`). `execDetails` does not (M5).
`FILLED` does not imply a quantity (M6). `ERROR` on socket drop is the
intended "down" mark (author), but it is then treated as a real broker
failure: waiters return, compensation runs, and persist makes it sticky
(`order_repository.py:108-115`) so reconnect cannot book the fill (M7).
Nothing times a working order out (M16). `_compensation_complete([])`
treating "we compensated nothing" as success (`coordinator.py:594-595`)
turns several of these into a quiet `COMPENSATED`.

Members: **M5**, **M6**, **M7**, **M16**.

### RC4 — In-memory RMS is a second ledger with no writer discipline

`RMSContext` (`open_positions`, `symbol_exposures`, `model_value_used`,
`margin_commitments`, `processed_signals`) is the live risk budget. It is
mutated by the signal path, hydrated by accumulation (not assignment),
ignored by kill-switch and pair-close, booked at a different price than it is
released, booked for `COMPENSATED` baskets that hold no risk, mutated from
the TWS thread without the asyncio lock, and keyed on un-normalized symbols.
The $10M strategy default makes the fail-closed check 8 that was supposed to
back-stop a missing row unreachable. Restart re-seeds from `positions` — when
`positions` is missing (M9, remainder-retry) or closed-but-wrong (M4), the
seed is wrong too.

Members: **M10**, **M11**, **M12**, **M19**, **M24**, **M27**, **M28**, **M14**
(first-clip notional — live once remainder-retry is on).

This is also why M17/M1 matter beyond double-submits: the only rebuild of RMS
is from a ledger that recovery cannot see.

### RC5 — There is no identifier normalization layer

`core/identifiers.py` claims total coverage and offers two helpers, imported
by one file (`worker_pool.py:35,38`). Symbol, account, strategy, and
signal-id are normalized in the instrument / margin / reconcile subsystems
and raw in the RMS / exposure / lock / P&L subsystems. The two meet in one
statement at `order_manager.py:987-991` (account uppercased, symbol not).
`:RETRY:` / `:CLOSE` / `:UNWIND:` are a second, string-surgery vocabulary
(`pnl.py:151` vs `normalize_trade_id`).

Members: **M27**, **M29**, **M14**, **M42**, **M43**, plus hygiene H9/H10/H28
(not in the master table).

A single `normalize_symbol` / `normalize_account` at the parser and at every
lock/budget key construction collapses M27, M29, M42 and the hydrate/write
key-shape split (H4).

### RC6 — The TWS thread and the event loop share mutable state without a shared lock

Two threads matter (`MAP.md` §1.4, §3.3): the `:8001` asyncio loop and
`TWSClientThread` (`tws_client.py:552`). Adapter maps are correctly under
`threading.Lock`. Everything else is opportunistic: margin commitments (M19),
`_next_req` (M30), `next_order_id` (M34), persist scheduling (M32), and a
blocking IB round-trip *on the loop* (M20). `except AttributeError: pass` on
the fill callback (M44) is the same boundary, failure-mode face.

Members: **M19**, **M20**, **M30**, **M32**, **M33** (pool exhaustion is how
the persist storm surfaces), **M34**, **M37**, **M44**, **M45**.

### RC7 — The kill switch is a cache plus a fire-and-forget task, not a durable worker

Arming is a process-local `set` (`kill_switch.py:45`) rehydrated once, after
other startup work, in a `try` that can skip it (M25). The flatten is not a
leased job, has no unique operation row, does not resume, does not re-check
the gate on the OPEN path, and counts its own compensation as success. It
deliberately has no `execution_claims` row (RC2). Completing flatten
(`COMPLETE` / `UNRESOLVED`) does not disarm — that part is deliberate and
correct — and a second square-off while armed is **refuse** (author: Start
Again first). `COMPLETE` with a live IB position is still indistinguishable
from `COMPLETE` and flat; the retry path is disarm then square-off again.

Members: **M2**, **M3**, **M4**, **M21**, **M22**, **M25**.

### RC8 — Config and docs fail open, with two sources of truth for the same knob

Shipped defaults send real orders (`BROKER_MODE` is ignored), leave the
webhook open (M8), and sign JWTs with a published placeholder. Model Blue
STK→CFD at submit is **intended** (author); M48 is the `paper_` /
`TEMPORARY` naming plus ledger STK vs executed CFD, not a wrong default.
Pacing numbers exist in `Settings` *and* as module constants (H22). The
emergency-reserve env var is a no-op and P1–P4 share one bucket — **author:
orders first**, so both faces of M31 are defects (P1 queue + honour or
delete the knob). `TRADINGAPP_TESTING=1` is an
open-admin bypass on any order-placing process whose DB name is not
exactly `ibkr_trading` (M55). Authoritative IB port is live Gateway
**4001**; `Settings` still defaults 7497 and `process_manager` still
waits on 4002 (M46). The accuracy table in `docs/README.md` labels this
whole tree ACCURATE.

Members: **M8**, **M31**, **M46**, **M48**, **M49**, **M55**, plus every P0/P1 in
`FINDINGS-docs.md` §1 that is drift rather than a code defect (see §6).

---

## 3. Fix order

A sequence, not a ranking. Earlier items make later items safe or even
meaningful. Items marked *parallel* do not depend on each other and can land
in the same window.

### Step 0 — Cheap confidence, off the order path (do first, this week)

These do not change how an order is built or submitted. They stop the
operator from making the next unforced error and give every later paper run
a trustworthy harness.

0a. Rewrite `backend/.env.example`: drop `BROKER_MODE`, add `DATABASE_URL`,
    `JWT_SECRET_KEY`, `WEBHOOK_AUTH_SECRET`, `EMERGENCY_KILLSWITCH_AUTH_SECRET`.
    Delete the accuracy table in `docs/README.md`. Banner or delete
    `POSTMAN_API_TESTING_GUIDE.md` and `docs/archive/production_mft_ibkr_pacing.md`.
    Cut `EC2_OPERATIONS_GUIDE.md` §5/§6/§8/§9/§11. Reason: an operator
    following current docs can start a second Gateway or believe orders are
    mocked. Independent of every code fix.
0b. **M8** — fail-closed webhook auth (startup assertion if
    `webhook_auth_enabled` and secret unset), same shape as `emergency.py:40-47`.
    Reason: ngrok is on `:8000` today. Config-only; no order-path change.
    Session-boundary deploy so TradingView is re-keyed first.
0c. **M43** — `o.intent.ibkr_account` on the three order routes. One-line,
    no behaviour change for admins, unblocks non-admin callers.
0d. **M54** — delete `_save_raw_capture_file`. Docs and
    `prune_webhook_captures.py` must stop hunting `webhook_*.json`;
    authority is `signal_jobs.capture_data`. The live CSV is **not**
    in this delete (H41 TEMPORARY). Also delete
    `record_rejected_inbound` and `get_open_by_strategy_symbol` (Q13).
    Keep `list_by_internal_order_id` / `weighted_average_price` (M13).
0e. **M50** — `systemctl disable --now process-manager` on the live host
    (author: systemd units only). Confirm `trading-backend.service` /
    `webhook-ingest.service` / `ibgateway.service` are the enabled set.
    Banner `scripts/process_manager.py` as deprecated; stop listing it in
    `AGENTS.md` / `README.md` / `docs/watchdog.md` as the supervisor.
    Watchdog already does not `systemctl` — keep it that way.
0f. **M46** — one port: **4001** (live Gateway). Set `Settings.ibkr_port`
    default and `.env.example` to 4001; if `process_manager.py` is kept at
    all, wait on 4001 not 4002. Watchdog is already 4001. Confirm the live
    `.env` has `IBKR_PORT=4001`. Does not turn remainder-retry on — that
    gate is `paper_retry_ports_allowed` and must stay closed until
    step 9 (M9/M14).
0g. **M48** — rename, do not retarget. CFD is production Model Blue
    (author); options come later. Strip `TEMPORARY` / `paper_` / `_DEMO`
    from `execution_override.py`, `PAPER_EXECUTE_STK_AS_CFD`,
    `apply_demo_override`, and the tests/docs that call it a paper map
    (`docs/safety.md:32`, `docs/backend-config.md`). Keep default True
    and the resolve-time STK→CFD map. Do **not** copy the map into the
    adapter. Persist or log executed `secType` so incident response does
    not reason from ledger STK.
0h. **M55** — `trading-backend` (any `placeOrder` process) must refuse
    `TRADINGAPP_TESTING=1` at startup. Grep systemd `EnvironmentFile`s /
    unit `Environment=` now. pytest/`conftest.py` stays the only setter.
    The `ibkr_trading` name guard is not sufficient. Demo `:8010`
    synthetic admin is the same flag — keep it off that unit too if the
    proxy can reach square-off (D#13).

### Step 1 — Identifier canon, before any RMS or lock change

1. **M27 + M29 + M42** — fold `.strip().upper()` into the Model Blue parser
   for symbols, and route every lock / budget / limit / duplicate key through
   `core/identifiers.py` (extend it; it is currently two functions and one
   importer). Backfill `positions.leg_*_symbol` and `per_symbol_limits.symbol`
   if any mixed-case rows exist. Reason: every later RMS fix (M10, M11, M24,
   M28) keys on these strings. Doing RMS first and normalize second double-
   writes the same buckets under two casings.

### Step 2 — Close the submit/persist lie, before touching recovery

2. **M1** — persist a durable "this intent is at the broker" fact *before*
   `placeOrder` returns, and teach `count_orders_emitted` (or its replacement)
   to treat a live `CLAIMED` claim as emitted. The least-surprising shape is
   inserting the `orders` row as `PENDING`/`SUBMITTED` with the reserved TWS
   id *before* the socket write, then updating on callback. Reason: M23's
   dead-letter arm, M38's quarantine undo, and M17's "we cannot see IB" are
   all answering the same question. Fixing recovery without fixing the
   predicate just changes *when* the double-submit happens.
3. **M38** falls out of (2) — once a `CLAIMED` claim is not releasable,
   startup cannot requeue a quarantined job. Do not patch M38 independently.

### Step 3 — Make the three flatten producers exclude each other (do not put `execution_claims` on them)

4. **M3** — partial unique index on `kill_switch_operations(account_id)`
   where status ∈ `_ARMED_STATUSES` (includes `COMPLETE` and `UNRESOLVED`;
   only `CLEARED` frees the slot). Widen `initiate_square_off`'s SELECT to
   the same set: if any armed row exists, return it with `created_new=False`
   so the HTTP handler does **not** start a second flatten worker. Author:
   second square-off while armed is refuse; leftover exposure is Start
   Again then square-off, or the sidecar. Operational cost: retrying
   `UNRESOLVED` briefly disarms OPENs. Does not by itself stop pair-close
   vs kill-switch (that is M2).
5. **M2** — one shared in-flight set (or table) keyed on the *position*, not
   on a per-attempt `uuid4`: `(account_id, trade_id)` for ledger closes,
   `(ibkr_account, con_id)` for broker-line flattens, with the two keys
   joined through `positions`. Second caller is returned "already flattening"
   (202 / existing operation), never a new `placeOrder`. After a crash, the
   lock is gone — retake is allowed **only after a broker snapshot says the
   qty is still there**, never after `count_orders_emitted == 0`. Do **not**
   call `_acquire_execution_claim` on these paths. Reason: M3 without M2
   still lets pair-close race kill-switch. Stable ids without exclusion still
   let two producers submit two MARKET reverses.
6. **M4** — exclude `is_compensation` (and `:UNWIND:`) from
   `filled_close`. Reason: only safe once (5) means there is at most one
   in-flight flatten per trade; otherwise you are filtering one of two
   overlapping operations.

### Step 4 — Make terminal mean terminal; park disconnect, do not compensate

7. **M5** — terminal guard on the `execDetails` status mutation, matching
   `:585`. Still *book* the late fill on the ledger (the money happened);
   do not regress status. The existing test in
   `tests/test_lifecycle_money_bugs.py` is the gate.
8. **M6** — `FILLED` requires `filled_quantity > 0`; `openOrder` with
   `Filled` and no qty stays `SUBMITTED`/`PARTIALLY_FILLED`. Same test file.
9. **M7** — keep the in-memory `ERROR` mark while disconnected (author).
   Change the aftermath: do **not** resolve the fill future as terminal; do
   **not** persist `ERROR` into `_TERMINAL_ORDER_STATUSES`; do **not**
   compensate or `COMPENSATED`. Park the basket until reconnect. Exclude
   disconnect-`ERROR` from the sticky persist at `order_repository.py:108-115`
   (a distinct `DISCONNECTED` status, or omit `ERROR` from that frozenset
   for this cause). R10 (pacing-timeout on cancel → ERROR) is unchanged —
   still stop treating a *failed cancel* as a failed order. (7) and (8) are
   parallel; (9) is the precondition of step 7 reconnect, not a substitute
   for it.
9b. **M35** — `worker_pool.stop()` waits on `has_in_flight_jobs()`;
    raise systemd `TimeoutStopSec` and the Gateway-bounce 5 s grace above
    `fill_timeout` (and reconnect wait). Author: drain in-flight baskets.
    Session-boundary. Land before reconnect so a Gateway bounce does not
    SIGKILL the park-and-reconcile path.

### Step 5 — Kill-switch as a worker, now that the barrier exists

10. **M21** — re-read `is_account_kill_switch_active` immediately before
    `_acquire_execution_claim` / `placeOrder`, and again in
    `_retry_incomplete` / `_compensate_filled`. Small, order-path.
11. **M22** — store the flatten task (copy `CriticalRecoveryService`'s
    `_in_flight` + done-callback). On startup, resume any
    `ACTIVATING`/`FLATTENING`/`RECONCILING` row. Depends on (4)/(5) so
    resume cannot double-fire.
12. **M25** — hydrate the kill-switch cache *first*, in its own `try`,
    before basket recovery. Independent, do in the same PR as (11).

### Step 6 — RMS becomes a projection of `positions`

13. **M28** — delete the `Decimal(10_000_000)` strategy default; propagate
    `accounts.default_symbol_limit` (and per-symbol rows) into
    `StrategyConfig`. After step 1 so the lookup casing matches.
14. **M24** — `hydrate_runtime_from_db` *assigns* (clear, then fill). Do
    this before trusting any post-restart paper run of (13).
15. **M10**, **M11**, **M12** — book and release exposure from the *entry*
    marks (or, better, rebuild `symbol_exposures` from `positions` after
    every terminal basket, flatten, and compensate). Kill-switch and
    pair-close must call the same rebuild and `LivePnlService.unwatch`.
    `COMPENSATED` must not go through `_record_unsettled_exposure`.
    Keep `max(Decimal(0), …)` at `:1308` / `:1246` / `:1262` as a
    safety net; if it trips after the rebuild, log it. Reason: three
    bugs, one function. Do not treat the floor as the M10 fix.

### Step 7 — Reconnect and adopt (the intended M7 recovery; after M1 and M7-park)

16. **M17** — wire `adopt_order` so `reqOpenOrders` / `reqExecutions`
    populate `_orders_by_tws_id`. This is the auto-reconcile on reconnect
    *and* on startup. Depends on (2): otherwise an adopted working order
    plus a released claim is a third submit. Depends on (9): otherwise
    adopted fills cannot overwrite sticky `ERROR`.
17. **M37** — set `coordinator._loop` in lifespan, not in `execute`.
    Independent, same PR as (16) is fine.
18. **M34 (reconnect)** — now required, not optional. After (16) and after
    putting `next_order_id` under one lock (C12). On success: adopt,
    replay executions, resume parked baskets, then M36 resubscribe.
    Until it lands, delete the log line at `main.py:112` so it stops lying.
19. **M36** — resubscribe market data on reconnect; meaningless before (18).

### Step 8 — Event-loop integrity (parallel with step 6; not on the submit path)

20. **M20** — `request_contract_details_async` / `to_thread` in
    `LivePnlService._request_ticks`. Highest-leverage loop fix; do not wait
    for reconnect.
21. **M19** — marshal margin-snapshot application onto the loop, or take a
    `threading.Lock` that both writers hold. And move
    `_assert_account_has_free_margin` inside `_exposure_guard`.
22. **M30** — same lock around `_next_req` and the req-id maps.
23. **M32** + **M33** — coalesce persist-on-callback (one pending write per
    `internal_order_id`); heartbeat exception sets `lease_lost`.
24. **M45** — `asyncio.to_thread` around `subprocess.run`.
25. **M44** — `hasattr` before `on_exec_details` / `on_order_status` (same
    shape as market-data `on_error`). Do **not** remove the skip: margin
    and position collectors share `_listeners` and do not implement
    those methods. An `AttributeError` *inside* the adapter must hit
    `except Exception` and log.

(20)–(25) are parallel with each other and with step 6.

### Step 9 — Remainder-retry identity, then lift the live gate

26. **M9 + M14** — **back in the live sequence.** Author: remainder-retry
    on live Gateway. Today `_retries_enabled` is False on 4001
    (`paper_retry_ports_allowed` = {7497, 4002}); that exclusion is a
    **blocker**, not the policy. **Author: remainder clip = same trade.**
    Strip `:RETRY:` the way `:UNWIND:` is stripped (`_persist_child` and
    `_ensure_signal_pk`); `_open_trade_from_fills` groups by `leg_index`
    not child-order count; exit marks and `_intent_with_fills`
    quantity-weight. Reason: `test_basket_retry.py` never crosses
    `after_submit`. Do **not** do this before (2) and (5) — a retried
    basket that crashes in the submit/persist window is M1 on a third
    identity. Do not keep `:RETRY:` in `orders.trade_id` or mint a
    second `signals` row.
26b. **Then** remove or invert the paper-port allowlist so 4001 retries
    (hygiene #42 was the opposite advice). Do not lift the gate in the
    same deploy as the persist fix. Dashboard knobs become live; docs
    that say "paper ports only" (`safety.md:16`, `backend-rms-oms.md`
    "Paper retries") are wrong.

### Step 10 — Remaining P1/P2, no further dependencies

27. **M18** — make the sibling `EXISTS` see in-flight claims: lock the
    `trade_id` (advisory, or a `trade_id` row), or claim OPEN+CLOSE as a
    unit, or hold the domain lock *before* the claim. Needs a
    reproduction against Postgres first (§5).
    **Also (Q15):** ingest must write `account_scope` via
    `DatabaseStrategyAccountRouter` (prefer `str(account_id)`). Today's
    omit is a defect. If the router returns N accounts, mint N jobs
    (one scope each) — a single job still fans out *after* one lock
    (`backend-concurrency.md`). Same-trade OPEN/CLOSE stay one lock;
    do not treat populate as the M18 fix.
28. **M13**, **M15** — P&L from the `executions` ledger
    (`weighted_average_price` / `total_commission` already exist and are
    unused). Refuse `close_trade` on incomplete marks. Entry commission
    stored on open or summed on close from both baskets.
29. **M16** — order/basket reaper; `EXECUTING` with no fills and an aged
    `updated_at` escalates.
30. **M26** — split kill-switch hydrate (already in 12) is the ordering
    fix; the remaining face is "don't let `run_startup_recovery` raise
    before the second `recover_incomplete_baskets`".
31. **M31** — **Author: orders first.** P0 flatten stays reserved. Give
    P1 a real waiting queue ahead of P3/P4 (stop equal polling). Honour
    `emergency_reserve` (`normal = max − reserve`) or delete the setting.
32. **M39**, **M40**, **M47** — independent.
    **Contract month (Q14)** — vestigial for CFD: delete `_STK_CONTRACT_MONTH`
    / kill-switch `"202612"` / pair-close `""`. `OrderLeg.contract_month`
    optional except FUT/FOP/OPT. Check 4 stays for those. Future OPT
    supplies a real expiry; do not invent a dummy for Model Blue CFD.
    **M41** — refuse OPEN if any `positions` row exists for that
    `(account_id, trade_id)`, including `CLOSED`. Do not mutate; do not
    insert a second cycle. Hydrate `processed_signals` from historical
    `signals` so check 2 matches that rule across restart.
    **M46** moved to step 0f (4001 everywhere). **M48** moved to step 0g
    (rename; mapping stays).
    (M35 moved to step 4 / 9b.)

### What not to sequence yet

- Reconnect-on-drop (step 7) before M1, M7-park (not "stop marking ERROR"),
  the `next_order_id` lock, and M35 drain. Reconnect is now *required*
  policy; those four are still preconditions, not reasons to skip it.
- `--workers 2` / a second trading process. Every in-process lock in
  `MAP.md` §5.3 / concurrency §"Critical sections" fails silently the day
  that happens. Webhook `execution_claims` would survive; flatten exclusion
  (M2) would not unless it is a database row, not a module dict. The job
  claim is subject to M18. Flatten must stay claim-free (author: emergency
  button), so a second trading process is unsafe until M2 is durable.
- Enabling `margin_whatif_enabled` / `market_value_check_enabled` /
  `margin_scan_enabled`. They add IB traffic and a lock (H38) on a path
  that is already starving P1 (M31).
- Keeping webhook ingest up 24/7 so Model Blue alerts queue overnight.
  Author: drop those (Q10). When other models need overnight accept,
  filter by strategy; do not lift the Model Blue drop.
- A create-user / `/register` API. Author: one-off Postgres insert (Q11).
- A create-strategy API. Author: same one-off SQL as `users` (Q19).
- `TRADINGAPP_TESTING=1` in any unit that can `placeOrder` (Q12 / M55).

---

## 4. Risk-of-fix assessment (top ten)

"Top ten" = the ten money-path changes that must land, in the order of §3
steps 2–7 plus M8. Not the ten cheapest.

| Fix | Touches order path? | Danger of the change itself | Rollout |
|---|---|---|---|
| **M1 — persist-before-placeOrder / claim-is-emitted** | **Yes.** Changes the only durable fact recovery trusts | Highest. A botched pre-insert can phantom-`PENDING` rows that block a real retry, or can fail to insert and leave the current double-submit in place. The claim state machine (`ABANDONED` retake at `execution_claim_repository.py:72-82`) is load-bearing and untested (`MAP.md` §7.3) | Paper-trading validation of: crash between persist and `placeOrder`; crash between `placeOrder` and callback; restart with a live `CLAIMED` claim and a working IB order; restart with a `CLAIMED` claim and nothing at IB. Session-boundary deploy. Do not ship on a weekday open |
| **M2 — flatten mutual exclusion + stable ids (no `execution_claims`)** | **Yes.** Kill-switch, pair-close, reconcile-flatten, critical recovery | Medium–high. Author: emergency button must never be refused by a stale webhook-style claim. Danger of the *wrong* fix is higher than the right one — wiring `_acquire_execution_claim` here would refuse flatten after M1's window. The remaining risk is a shared lock that sticks after the broker is already flat (blocks a needed second flatten) or that vanishes before the first flatten submits (today's double reverse) | Paper: two concurrent square-offs, square-off vs pair-close, square-off, kill process, square-off again with a live leftover, square-off again when IB is already flat (must not reverse). Sidecar `scripts/oms/flatten_gateway_positions.py` stays the break-glass that bypasses even this exclusion |
| **M3 — unique armed operation** | No submit change; insert path only | Low. Predicate is now settled: `_ARMED_STATUSES` including `COMPLETE`/`UNRESOLVED`. Wrong direction would be omitting those two (today's SELECT), which allows a second flatten after the worker finished. HTTP can keep returning the existing operation (today's duplicate-activation shape) as long as `created_new=False` so no second worker starts | Migration can apply mid-session. Widen the SELECT and add the unique index in the same deploy. Paper: square-off, wait until `COMPLETE` or `UNRESOLVED`, square-off again — no new `placeOrder`. Then Start Again, then square-off — new operation allowed |
| **M5 — execDetails terminal guard** | **Yes.** Fill callback | Medium. The wrong guard *drops the fill* rather than dropping the status regression. Book the fill, keep status. The reproducing test already encodes the distinction | Paper, then live. Can ship in a session if the test is in CI. Low blast radius *if* the fill is still persisted |
| **M6 — FILLED requires quantity** | **Yes.** `openOrder` / `orderStatus` | Medium. An IB `Filled` with qty arriving on the next tick must still settle. The risk is leaving an order non-terminal forever (feeds M16) if qty never comes. Pair with a timeout, or accept that M16's reaper is now required | Same paper window as M5. Ship together |
| **M7 — park on disconnect, no sticky ERROR, no compensate** | **Yes.** Disconnect path, coordinator wait, order persist | High. Author wants ERROR-while-down *and* auto-reconcile-when-up. Today's ERROR is terminal: waiters return and persist will not accept a later fill. The wrong fix is deleting the mark. The right fix is: mark down, do not compensate, persist a non-sticky state, reconnect (M34) + adopt (M17). A botched persist change can leave FILLED orders stuck ERROR forever, or can let a real broker reject be overwritten | Session-boundary, with M35 drain in the same window. Paper: drop the socket mid-basket, reconnect, assert fills book and no `BASKET_COMPENSATED`. Do not ship reconnect in the same commit as the persist change |
| **M8 — webhook fail-closed** | No (ingest only) | Low for the trading process. High for *operations*: the next TradingView alert 401s until the secret is set and the alert header is updated. Defaults today are open, so production may have no secret at all | Set the secret, update the TradingView header, *then* deploy the assertion. Off-hours. Confirm with a probe POST that 401s without the header and 202s with it |
| **M4 — ignore compensation in KS reconcile** | Yes (KS finalize only) | Medium. Changes when a pair is marked `CLOSED` and when the operation is `COMPLETE`. If compensation is the *only* FILLED `KILLSWITCH-` row (weird but reachable under M5), the pair would stay OPEN — the safe direction | Paper: one-leg-filled flatten. Assert `risk_state` stays `OPEN` and status `UNRESOLVED`. Can follow M2/M3 in the same week, not the same commit |
| **M21 — re-check kill switch before submit** | **Yes.** Every OPEN, plus retry/compensate | Medium. A true positive (OPEN aborted after RMS passed) leaves a sealed-or-unsealed claim and no order — the M1-safe direction if step 2 landed first. A false positive (stale cache) aborts a good OPEN. Cache is process-local and armed in-process, so stale is the *missing* arm (M25), not a stale true | After M25. Paper: arm mid-instrument-resolve, confirm the OPEN is refused and no `placeOrder` |
| **M17 — adopt_order** | **Yes.** Startup and reconnect auto-reconcile | High. Adopting the wrong TWS id maps callbacks from order A onto order B (the C12 failure mode). Must key on `permId` / `(conId, side, qty)` with an explicit unmapped remainder, not "first unused slot" | Paper restart *and* mid-basket disconnect+reconnect. Session-boundary. After M1 and M7-park. Same window as M34, not before it blindly |
| **M9 / M14 — retry persist + identity (before lifting the live gate)** | **Yes.** After-submit persist and remainder clips | High if the paper-port allowlist is lifted first: every incomplete live basket hits `POSITION_REQUIRES_FILLS` and a split `trade_id`. Medium after identity is fixed: grouping by `leg_index` can hide a real extra leg. **Author: same trade** — stripping `:RETRY:` into the parent is the intended direction (wrong would have been treating clips as distinct trades) | Paper with retry on, then live. Land M9/M14 (strip `:RETRY:`, group by `leg_index`, quantity-weight), then 26b (allow 4001). Never the reverse |

Author: remainder-retry **is** intended on live. M9/M14 are therefore live-money preconditions of turning retry on. The port allowlist currently hides them; do not treat that hide as a fix.

---

## 5. Confidence audit

Every finding the source sessions labelled **speculative**, plus the
**likely** rows I promoted into the master table, with a kill-shot that is
cheap enough to run this week. "Kill" means the observation would let you
drop or demote the row without a code change.

### Speculative (source label)

| ID | What would confirm | What would kill |
|---|---|---|
| **M41** (L18, A6) | Author settled: unique forever. Remaining hole: OPEN after CLOSE still mutates. Confirm hydrate: a second OPEN of a closed `trade_id` after restart must `DUPLICATE_SIGNAL` / `MODEL_BLUE_DUPLICATE_OPEN` | A6 as "ids recycle in production" is **rejected**. Zero `signals` groups with count>2 is hygiene, not a kill of the persist_open hole |
| **M51** (L15, A5) | `SELECT exec_id, commission, commission_currency FROM executions WHERE commission < 0 LIMIT 20`. Or one `commissionReport` log line with a negative `commission` | Zero negative commissions across the `executions` table *and* a month of `commissionReport` logs kills A5 for this account type |
| **M52** (L20, A7) | `SELECT exec_id FROM executions WHERE exec_id LIKE 'noid:%'`. The fallback exists at `ibkr_adapter.py:807-809`, so a single row confirms IB delivered an empty `execId` | Zero `noid:` rows over the life of the DB kills the trigger. Keep the fallback; drop the finding |
| **M53** (L25, A8) | `SELECT trade_id, count(DISTINCT commission_currency) FROM executions e JOIN orders o ON e.order_id=o.id GROUP BY 1 HAVING count(*) > 1` | One currency across all `executions.commission_currency` (including NULL-as-USD) kills it as a live bug. Keep as a latent trap |
| **M34 / C12** | A second `nextValidId` log line after the process has already submitted, or a Gateway reconnect in `trading.log` | `grep nextValidId storage/logs/*/trading.log` showing exactly one per process lifetime, and no reconnect path landing, keeps this latent. It becomes real the day reconnect is added — treat the lock as a precondition of that PR, not as a live P2 |
| **M49** (R14) | `Error 100` in `trading.log` within 30 s of a `Started` / `connect_and_start` line, clustered on restart days | A month of restarts with no Error 100 in the first minute kills the practical concern. The full-bucket init remains a fact |
| **M50** (R20) | `systemctl is-enabled process-manager trading-backend` on the live host; `ss -lntp \| grep 8001` showing two listeners | `process-manager` disabled and not running **confirms** the host matches intent. The unit file still existing is fine; **enabled** is the bug |

### Likely (promoted; still need a kill-shot)

| ID | What would confirm | What would kill |
|---|---|---|
| **M6** (A2) | Already reproduced in-process. Confirm *in production* with `grep -E 'openOrder.*Filled\|Ignoring.*unknown tws_id' storage/logs/*/trading.log` and a matching `orders` row `status='FILLED' AND fill_qty=0` | A week of `openOrder` callbacks none of whose mapped status is `FILLED` before a qty-bearing `orderStatus` demotes this to latent. The code path stays |
| **M5 / A1** | Already reproduced. Production: `orders` row `status='CANCELLED' AND fill_qty > 0` with no `is_compensation` sibling, `PARTIAL_FILL` event after `BASKET_COMPENSATED` | Zero such rows over the life of the DB *and* no `execDetails` log after a `CANCELLED` status line. Unlikely — this is a known IB ordering — but that is the kill |
| **M7 latch-clear face** | `BASKET_CRITICAL_CLEARED` + `No filled non-compensation legs` in `trading.log` on the same timestamp as `Connection closed unexpectedly`, with a `broker_positions` qty > 0 for that account | That pair of lines never co-occurring. The COMPENSATED-with-live-IB face is already certain from `_compensation_complete([])` and does not need this |
| **M18** (C4) | A unit test that opens two connections, holds OPEN's claim transaction open, lets CLOSE's `claim_next_jobs` run, then commits both. Expected today: both claimed. Or production: a `REJECTED` job with `NO_OPEN_POSITION` whose `received_at` is after its sibling OPEN's `received_at` by less than 1 s | That test, against the real `ibkr_trading_test` Postgres, showing CLOSE *blocked*. Then the docstring matches the implementation and the finding dies |
| **M27 trigger** | One day's raw webhook payloads (`signal_jobs.capture_data` or the CSV) with two distinct casings of the same underlying. `SELECT distinct capture_data->…` | A month of payloads whose `underlying` is uniformly the casing the `instruments` / `per_symbol_limits` rows use. Demote to latent; still do the parser fold, it is cheap |
| **M9 / M14 / A3** | Author: remainder-retry on live. Code path is certain; currently gated off on 4001. Confirm after 26b with `SELECT trade_id FROM orders WHERE trade_id LIKE '%:RETRY:%'` | The allowlist staying forever would have killed this as live-money. That is no longer policy. Keep as live P0/P1; still empty `:RETRY:` rows **until** the gate is lifted |
| **M23 worker-beats-reclaimer** | `Worker %s LOST its lease` in `trading.log` whose job later has FILLED orders and a `COMPLETED` *or* `RECOVERY_REQUIRED` status | Zero lease-loss lines over a busy week. The claim-query vs docstring contradiction remains a code fact |
| **M25 / M26 fire** | `Failed to hydrate Model Blue/RMS runtime state` without a following `KILL SWITCH REARMED FROM DB`; or `BASKET_RECOVER_DEFER` with no later `BASKET_RECOVER_CRITICAL` on a restart that had `baskets.state='EXECUTING'` | A sample of restarts (the session-stop timer is a weekly source) where hydrate always completes and the second recover pass always runs. Ordering is still wrong |
| **M30** | `/demo/market-data-health` showing a `LIVE` contract whose `last_mark` is the other leg's price, correlated with a `rerouteMktDataReq` log line | No CFD reroute on this account type (STK-only). Then the TWS-thread increment is unreachable and C8 dies |
| **M31 starvation** | Author: orders first — starvation is a defect. Confirm with `Gateway pacing timeout` on a second leg within a minute of process start / market-data resubscribe, `BASKET_UNWINDING` | A month of opens with no pacing-timeout demotes the *trigger*, not the missing P1 queue. Dead `emergency_reserve` remains certain |
| **M32 pool** | `Worker %s failed to renew heartbeat` clustered with a fill burst, `pool_timeout` / checkout errors in the same second | Pool wait-time metrics (or a log line — there may be none) staying well under 30 s during a known burst. Then C10 is theoretical at current volume |
| **M40 / A9** | `SELECT symbol, size_increment FROM instruments WHERE size_increment > 1`. Or a close `orders.quantity` strictly less than `abs(positions.leg_a_signed_qty)` for the same trade | Every `instruments.size_increment` is NULL or 1. The missing cross-check remains; the live divergence dies |

### Already certain, no audit needed

M1–M4, M8–M14, M16, M17, M19–M22 (no-resume face), M24, M28, M29, M31
(collapse vs intent), M33, M35–M37, M39, M41, M43–M48, M54, M55. M9/M14 are certain and **live-money once the
4001 retry gate is lifted**; dormant only while `paper_retry_ports_allowed`
excludes 4001. Several of these have reproducing tests
(`tests/test_lifecycle_money_bugs.py` for M4/M5/M6). M1 does not, and
`execution_claims` has no test at all (`MAP.md` §7.3) — that is a coverage
hole, not a confidence hole on the read of the code.

---

## 6. Documentation verdict

### Code defects from the doc audit — already in §1

`FINDINGS-docs.md` §3 is a bug list, not drift. Placement:

| Docs-audit ID | Master ID | Notes |
|---|---|---|
| §3.1 webhook fail-open | **M8** | P0 |
| §3.2 $10M strategy default | **M28** | P1 |
| §3.3 emergency-reserve no-op | **M31** | merged with C9 |
| §3.4 three gateway ports | **M46** | Author: 4001. Settings/process_manager defaults still wrong |
| §3.5 dead JSON capture + useless pruner | **M54** | Author: delete the JSON helper; capture_data stays |
| §3.6 `GET /health/ready` always 200 | **dropped** (§7) | Deliberate; watchdog reads the body |

### What to do with each document

Adopted from `FINDINGS-docs.md` §2, with no re-audit. The accuracy table in
`docs/README.md:47-71` is itself finding D#16 and is not to be trusted as a
router.

**Fix in place** (the document is the right artifact; it is wrong in named
places):

| Document | Why keep | What to fix |
|---|---|---|
| `docs/backend-concurrency.md` | Strongest doc in the tree; leases, claims, eight "do not break" rules hold | Note the empty-`signal_id` UUID fallback (D#40); note that `execution_claims` and the lease reclaimer are untested. **Q15:** `account_scope` is **not** reserved — ingest must populate it today; the "optional per-account child jobs / not built" line is now the intended fix |
| `docs/backend-kill-switch.md` | Best runbook; sidecar claims verified | State that flatten paths deliberately skip `execution_claims`. State the square-off vs Start Again rule: second square-off while armed (`COMPLETE`/`UNRESOLVED` included) is refuse; leftover exposure is Start Again then square-off, or the sidecar. Add M2/M3/M4 as known holes until fixed; columns of `kill_switch_operations` |
| `docs/backend-rms-oms.md` | Check order, bands, basket states, `managedAccounts` gate are right | Check 7/8 fail-closed (D#11) and the $10M hole (M28); `cancel_timeout` not overridable (D#33). "Paper retries" / live-ports-never is **wrong intent** (Q7). **Q16:** P1 orders ahead of P3/P4; P0 flatten first (M31). **`trade_id` unique forever** (Q9); no flip — opposite direction is a new OPEN with a new id (Q20). **Q14:** `contract_month` optional except FUT/FOP/OPT |
| `docs/backend-config.md` | Fine for the fields it has | Add `jwt_*`, state the webhook fail-open (until M8), state the emergency-reserve no-op (until M31); `IBKR_PORT` default is **4001** (live Gateway), not 7497. `paper_execute_stk_as_cfd` is **production Model Blue** (STK alert → IBKR CFD), not paper/demo (M48). **Q12:** `TRADINGAPP_TESTING` never on a `placeOrder` process (M55) |
| `docs/backend-persistence.md` | Migration chain and head are right | Add `users`; add `accounts.default_symbol_limit`; Redis-import correction (D#32); drop `list_by_internal_order_id` / `is_processed` from the live surface until they are. **Q11 / Q19:** `users` and `strategies` rows are one-off operator inserts; no app writer by design |
| `docs/safety.md` | Pacing section is the best writing in the repo | Auth on `/demo/*` exists; the `:8010` proxy has none (D#17, D#13); JSON capture is **deleted** (Q13 / D#22) — payload is `signal_jobs.capture_data`; no inline webhook path (D#23). `:16` "live ports never get those retries" is **wrong intent** (Q7). `:32` still calls STK→CFD a paper/demo map — it is production Model Blue (M48). **Q10:** Model Blue alerts outside RTH are dropped at TCP. **Q16:** P1 orders ahead of P3/P4 (M31); P0 flatten stays first |
| `docs/backend-execution.md` | Log-grep table is immediately useful | Redraw the live path: margin gate, kill-switch *after* `build_intent`, what-if, CRITICAL gate (D#18, D#19). Drop the legacy inline path and `rejected_by_rms` |
| `docs/backend-map.md` | Useful package index | Alembic head `h2i3j4k5l6m7`; auth/`deps.py`; lifespan (margin scan, enqueue critical); limiter values *are* Settings (D#25, D#28, D#29, D#30) |
| `docs/backend-api.md` | Every listed route exists | Add auth, `auth_*`, `service-control`, pair-close, health live/ready; proxy is `/api/v1/*` not `config/*` (D#12, D#13). No register/create-user route — intentional (Q11). No create-strategy route — same (Q19); config API only validates `allocations` against existing `strategies` rows |
| `docs/watchdog.md` | State machine and budget are right | Gateway port **4001** (author: live Gateway only). Ownership: systemd units only — process_manager does not own restarts; watchdog does not `systemctl`. D#14, D#15. Session-stop of ingest at 16:00 ET is **intended** for Model Blue (Q10) |
| `docs/backend-testing.md` | Commands and the test-DB story are right | Inventory is 95 files, not 48 (D#31). `TRADINGAPP_TESTING=1` is pytest-only (Q12); never in systemd for `:8001` |
| `docs/OPERATOR_TELEGRAM_ALERTS.md` | Newest; semantics hold | One number: **4001** |
| `AGENTS.md`, `README.md` | Invariants they *do* state hold | Silence on auth and systemd; still present `process_manager.py` as the run path — author: systemd units only, so those commands are the wrong ops story. 20 tables not 15; table is `event_log` not `events`; self-certified commit is 19 commits behind (D#26, D#27) |
| `backend/.env.example` | Something has to be the template | Findings D#1 and D#3. Highest-consequence artifact per byte. **Fix before any other doc.** `IBKR_PORT=4001`. Never `TRADINGAPP_TESTING=1` (M55). Do not delete |

**Rewrite from `MAP.md`**, do not patch:

| Document | Why |
|---|---|
| `docs/backend-multi-gateway.md` "As-Is" tables and the gap-analysis at `:325-342` | Declares `managedAccounts`, Error 100 handling, flatten priority, and `reqMktData` pacing *absent* — all implemented — and contradicts itself inside the file (D#7–#10). The target/phase/open-questions half is worth keeping. Delete As-Is; link `MAP.md` §3.2 / §5.5 and `backend-rms-oms.md` |
| `docs/gaps.md` | Comparison target `Execution_System_Architecture.md` does not exist (D#24). Rewrite as "not implemented, vs `MAP.md`" or delete |
| A gateway-failure runbook (does not exist) | Facts are scattered across `safety.md:90`, `ibkr_adapter.py:1017`, `backend-kill-switch.md:131`. Nothing tells an operator what to do in the first minute of a socket drop. Write this from `MAP.md` §3.3 + M7 + M17, not from the EC2 guide |
| Auth / systemd / process-to-DB matrix (do not exist) | `FINDINGS-docs.md` §4 items 1, 7, 10. These are the three biggest silences. `MAP.md` §1.3, §1.5, §2.1–2.3 is the source |

**Delete outright** (reading them is worse than reading nothing):

| Document | Why |
|---|---|
| `docs/README.md` §"Doc inventory (accuracy vs code)" and its two changelogs | D#16. A stale ACCURATE stamp launders eleven wrong documents. Keep the router half of the file |
| `docs/EC2_OPERATIONS_GUIDE.md` §5 (tmux), §6 (start procedure), §8 (what the backend does), §9, §11 (pre-order checklist) | D#5, D#6, D#36. Following §6 starts a second uvicorn and a second Gateway login — the same file's never-do list. The banner buys credibility for the parts that are wrong. Keep SSH / IBC / Jts / secret paths / the never-do list; delete the rest rather than patching |
| `backend/POSTMAN_API_TESTING_GUIDE.md` | D#35. 847 lines of an API that does not exist, `BROKER_MODE` as the organising concept, **no in-file banner**. Five other docs warn about it; readers who arrive by search never see those warnings. Archive-with-banner is the fallback, not a reason to keep it on the path |
| `docs/archive/production_mft_ibkr_pacing.md` | D#34. Competing ceiling (40 vs 30/50), deleted class, inverted priority ladder, sample that does not parse. `backend-multi-gateway.md` already carries the surviving intent |
| `docs/backend-multi-gateway.md` As-Is / gap tables | See rewrite row. If the rewrite does not happen, delete those sections rather than leave them |

**Do not delete, do not treat as current:**
`backend/docs/DEVELOPER_EXECUTION_GUIDE.md` (already has a stale banner),
`docs/archive/*` other than the pacing note, `pair-allocation-and-model-market-value-spec.md`
(completed build plan still in the imperative; harmless outside the repo).

`docs/review/MAP.md` is the only artifact the doc audit trusted, with one
correction: Redis *is* imported under `backend/app/services/system_monitor_service.py:18`
and lazily in the watchdog. Not on the order path. The useful claim is
"Redis is not on the order path", not "no Redis import under `backend/app/`".

---

## 7. Dropped

Findings I am willing to disagree with. The earlier sessions had narrower
scope; several things that look like defects from one angle are deliberate,
inert, or overstated once the six reports sit together.

| Source | Why it is not a live defect |
|---|---|
| **L17** — reconciler classifies and does not repair | Deliberate. Module docstring at `position_reconciler.py:1` says snapshot-and-log. It is the *only* sensor for M1–M4 and M7, which makes it operationally important and still not a bug. Do not "fix" it by auto-flattening until M2's mutual exclusion exists (auto-flatten today is a second producer racing the operator) |
| **L19** — legacy single-name `int()` truncating into `open_positions` | Dead. Unreachable whenever `_account_router` is set, which `session_factory` guarantees (`order_manager.py:136-138`, raise at `:699-702`) |
| **L21** — `_recompute` sums two legs | Latent. `positions` is a hard two-leg schema (`position_repository.py:143-144`). Not a live bug |
| **C7 GC of the flatten task** | Overstated. CPython 3.11+ keeps a strong reference to tasks the loop has scheduled. The unretrieved-exception and no-resume faces are real and live on as **M22**. The "GC collects the flatten before it runs" interleaving is not |
| **C14** — `InvalidStateError` on a cancelled future | No state corruption. The timeout path re-reads the live order (`ibkr_adapter.py:544-546`). Log noise |
| **C15** — `submit_one_leg` records `_submitted_signals` and never reads it | MAP §5.4 overstates the inventory. The adapter map at `ibkr_adapter.py:397-401` is the real in-process guard and is sound. Not a defect |
| **D§3.6** — `/health/ready` returns 200 on TWS disconnect | Deliberate; comment on the route and `docs/watchdog.md:24` both say the watchdog reads the body. Not a probe for `curl -f`. Document it next to the route; do not change the status code unless systemd starts keying on it |
| **H20** — `_exit_marks_from_orders` computed twice in one close | Pure function over a fixed list. Harmless |
| **H33, H34, H36, H37, H40** — aliases, a constant `delayed=False`, unused ceiling constant, unused `enabled=` | Bloat. None of them change a submit |
| **H38** — three subsystems default off | Product flags, not defects. Do not enable them as a "fix" |
| **Hygiene P0 on M27** | Facts kept, severity dropped to P1. See §1 |
| **Hygiene #42 "hide knobs on live"** | Wrong direction. Author: remainder-retry on live. The paper-port allowlist is a blocker to lift *after* M9/M14, not a feature to harden |
| **Ingest stop at 16:00 ET (resilience Q2 / MAP §1.5 timers)** | Intended for **Model Blue** (author: drop, do not queue). TCP-level loss is policy, not a bug. Future other models may be accepted overnight — then filter by strategy; do not "fix" by leaving ingest up for Model Blue. **M35** (trading process SIGKILL mid-basket at the same timer) is a separate defect and stays |
| **Lifecycle P2 on M7** | Mark-while-down is intended (A4). P0 kept for sticky persist + compensate-before-reconnect + missing reconnect. "Stop assigning ERROR" is the wrong fix |
| **MAP §2.3 no `users` writer** | Intentional. Author: one-off insert on this host, no ongoing path. Do not add `/register`. Document the operator INSERT |
| **Dummy `contract_month` on CFD** | Author: vestigial. Delete the three literals; field required only for expiry instruments. Check 4 stays for OPT/FUT. Not a live money bug on CFD as long as check 4 ignores empty/non-expiry |

Deletion candidates in `FINDINGS-hygiene.md` Part B — **author Q13: delete
these three now:**

- `_save_raw_capture_file` (`webhooks.py:59`) — retired; payload is
  `signal_jobs.capture_data` (M54).
- `record_rejected_inbound` (`order_manager.py:1342`) — never wired;
  parse rejects are not a lost-signal regression to restore.
- `get_open_by_strategy_symbol` (`position_repository.py:79`) — dead
  query; M10/M11 stay in-memory `symbol_exposures` until those fixes,
  not this unread SELECT.

**Do not delete** (M13 still needs them):

- `list_by_internal_order_id` / `list_by_internal_order_ids` /
  `weighted_average_price` / `realized_pnl_from_marks`

---

## 8. Questions for the author (all settled)

No amount of reading the code settled these. All twenty answers are
below. Several of the P0 fixes in §3 forked on them; those forks are
closed.

### Settled

1. **Skipping `_acquire_execution_claim` on flatten paths is deliberate.**
   The emergency button must never be refused by a stale barrier row.
   **M2 narrowed:** do not put `execution_claims` on kill-switch, pair-close,
   or reconcile-flatten. Remaining defect is that the three producers do not
   exclude each other; any replacement lock is retakeable only after a
   broker snapshot says the position is still live, never after
   `count_orders_emitted == 0`. RC2, §3 step 3, and the M2 risk row updated.

2. **Second square-off while armed is refuse**, including `COMPLETE` and
   `UNRESOLVED`. Operator must Start Again (`POST .../kill-switch/clear` →
   `CLEARED`) before a new flatten. M3 unique predicate = `_ARMED_STATUSES`
   (only `CLEARED` frees the slot). Today's SELECT omits `COMPLETE`/
   `UNRESOLVED`, which is now a confirmed bug against intent. Retrying
   leftover exposure therefore requires a brief disarm. M3 row, §3 step 4,
   and the M3 risk row updated.

3. **Socket down → mark `ERROR`; socket up → auto-reconcile. Shutdown
   waits for in-flight baskets.** A4 confirmed for the *mark*, not for
   treating ERROR as a broker failure. M7 fix is park / non-sticky persist /
   no compensate, then M34 reconnect + M17 adopt — not "stop assigning
   ERROR". M34 bumped to P1. M35 is a confirmed defect (`has_in_flight_jobs`
   must gate stop; `TimeoutStopSec` above `fill_timeout`). R10 (cancel
   pacing → ERROR) was not part of this answer.

4. **Systemd units only own restarts.** Disable `process-manager.service`.
   Watchdog stays observe/notify (it already does not `systemctl`). M50 is
   a host misconfig if `process-manager` is enabled. M35's 5 s
   `_ensure_fastapi` grace becomes irrelevant once that unit is off;
   systemd `TimeoutStopSec` is the remaining drain knob. Step 0e added.

5. **Live Gateway only — port 4001.** `IBKR_PORT=4001` is the single
   authority. Watchdog default already matches. `Settings.ibkr_port`
   default 7497 and `process_manager` 4002 are wrong (M46 → step 0f).
   `paper_retry_ports_allowed` currently keeps remainder-retry **off** on
   this host (step 0f must not lift that). Confirm `.env` has
   `IBKR_PORT=4001`.

6. **CFD is production Model Blue.** TradingView still sends `STK`;
   submit maps to IBKR CFD. That mapping stays (`default True`); do not
   disable it as a "paper" flag. Options will be added later as a new
   requested instrument, not a replacement of this map. **M48 narrowed
   to naming + persist mismatch:** strip `TEMPORARY` / `paper_` /
   `_DEMO`; persist or log executed secType; keep the map at resolve
   time (do not copy into the adapter). Step 0g. Docs that still say
   paper/demo (`safety.md`, `backend-config.md`) are wrong.

7. **Remainder-retry is intended on live.** The paper-port allowlist
   (`{7497, 4002}`) is a scaffold, not the live safety policy. **M9 and
   M14 return to the live-money sequence.** Do not add 4001 to the
   allowlist until persist/identity are fixed (step 9 then 26b). Hygiene
   #42 ("hide the knobs") is the wrong direction. Docs that say live
   ports never retry (`safety.md:16`, `backend-rms-oms.md`) are wrong.

8. **`:RETRY:` clips belong to the original trade.** Strip `:RETRY:` like
   `:UNWIND:` on persist so `orders.trade_id` and `signals` stay the
   parent id. Kill-switch / CLOSE / RMS already key that id — that is
   now confirmed correct. M14 identity face is a bug. M9 (3-order
   persist crash) is unchanged. Step 9 no longer forks.

9. **`trade_id` is unique per account for the life of the book.** Reuse
   (Pine recycle, strategy restart) is a TradingView bug, not a second
   cycle. Duplicate check 2 refuse is intended after CLOSE. Do **not**
   version `positions` rows. **M41 narrowed:** `_build_open_intent` only
   refuses OPEN (`get_open`); persist_open mutates CLOSED. Both must
   refuse if any row exists. Hydrate `processed_signals` from history
   so restart cannot reopen. A6 ("ids recycle in production") rejected.
   **Q20:** a trade does not flip; opposite direction is a new OPEN with
   a different `trade_id`.

10. **Drop out-of-hours Model Blue alerts.** Session-stop of ingest at
    16:00 ET (TCP drop, not queue) is intended for Model Blue: no alert
    we were not standing in front of. Do **not** keep ingest up 24/7 as
    a "fix". **Future:** other models may be accepted overnight — then
    stopping the whole ingest process is too coarse; accept by strategy
    (Model Blue still dropped off-hours). Write the Model Blue rule down
    now (`safety.md` / session-timer docs). Not a live defect.

11. **`users` rows are a one-off insert on this host.** No ongoing path
    and no create-user API — that is intentional. Document: operator
    inserts into Postgres (`users.email` unique, `role` admin/non-admin).
    MAP §2.3 "no application writer" is policy, not a missing feature.
    Do not add `/register`. The insert that already exists is the
    security boundary; keep it off the webhook/ngrok surface.

12. **`TRADINGAPP_TESTING=1` never on a process that can place orders.**
    pytest/`conftest.py` is the only legitimate setter. The DB-name
    guard (`ibkr_trading` only) is not enough — **M55** (H23 promoted
    P1). `trading-backend` startup must refuse the flag. Grep unit
    `EnvironmentFile`s (step 0h). Demo `:8010` uses the same bypass;
    keep it off that unit if the unauthenticated proxy can reach
    square-off (D#13).

13. **Delete all three unused capture/query pieces.** Disk JSON
    (`_save_raw_capture_file`) is retired; authority is
    `signal_jobs.capture_data`. `record_rejected_inbound` was never
    wired — do not restore. `get_open_by_strategy_symbol` is a dead
    query — M10/M11 do not depend on it. Step 0d. Fill-ledger readers
    (`weighted_average_price` etc.) stay for M13.

14. **Contract month is vestigial for CFD.** Delete `_STK_CONTRACT_MONTH`
    `"2026-09"`, kill-switch `"202612"`, pair-close `""`. Make
    `OrderLeg.contract_month` optional except FUT/FOP/OPT. Check 4
    stays for those. Future OPT supplies a real expiry — do not keep a
    dummy on Model Blue CFD. Step 10.

15. **Ingest must populate `account_scope` today.** Not reserved. Write
    it from `DatabaseStrategyAccountRouter` (prefer `str(account_id)`).
    Omitting it is a defect: every Model Blue job shares `("default",
    strategy_id)`. If N accounts match, mint N jobs — do not keep
    fan-out after a single strategy-wide lock. **Does not fix M18:**
    same-trade OPEN/CLOSE still share a lock; the claim sibling
    `EXISTS` still must see in-flight rows.

16. **Orders first.** P1–P4 collapse is **not** intended. P0 flatten
    stays reserved. P1 (`placeOrder`/`cancelOrder`) must queue ahead of
    P3 market data and P4 diagnostics. Honour `emergency_reserve` or
    delete the setting. **M31 bumped P2 → P1.** Step 10 item 31.

17. **Fill-path `except AttributeError: pass` is copy-paste, skip is
    load-bearing.** Verified: mixed `_listeners` (adapter + margin +
    position collector). Skipping missing methods is required. Using
    `except AttributeError` (vs `hasattr`, already used on market data)
    was copied in `6403eb3` / `902ca4d2` and would hide a bug *inside*
    `on_exec_details`. **M44:** `hasattr` then call; log other
    exceptions. Author agreed accidental; code confirms.

18. **Keep the exposure floor as a safety net.** Do not raise when
    CLOSE would drive `symbol_exposures` below zero. Fix M10 by
    booking/releasing from *entry* marks (or rebuild from `positions`).
    After that, `max(Decimal(0), …)` stays so a leftover leak cannot
    store a negative and poison `MONEY_PER_STOCK`. If the floor trips,
    log it. Step 6 item 15.

19. **`strategies` rows are a one-off insert, same as `users`.** No
    runtime writer and no create-strategy API — that is intentional.
    Document: operator inserts into Postgres (`strategies.strategy_id`
    unique; Model Blue needs `enabled`, `legs`, `max_open_positions`,
    `weight_source`). Config API reads the table to validate
    `allocations` (`UNKNOWN_STRATEGY` if missing). MAP §2.2 "no runtime
    writer" is policy, not a missing feature. Do not add a create
    endpoint. Finding D#11 is a doc gap.

20. **A trade opens, then closes. It does not flip.** `trade_id` is
    unique (Q9). Opposite direction is a **new OPEN** with a **different**
    `trade_id`. `MODEL_BLUE_DUPLICATE_OPEN` is the intended reject of a
    second OPEN on a live id; parser stays OPEN/CLOSE only — no `FLIP`.
    Write this in `safety.md` / Model Blue docs. Not a defect.

No remaining author questions. Judgment in §1–§7 stands.