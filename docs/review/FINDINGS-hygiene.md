# FINDINGS — Consistency & Bloat

Scope: consistency and bloat only. Individual logic bugs are out of scope except
where the bug *is* the inconsistency. Formatting and import-order nits skipped.

Severity: **P0** can lose money or duplicate orders · **P1** can corrupt state or
lose signals · **P2** operational fragility · **P3** maintainability.

Confidence labels: **certain** (read the code, greps closed), **likely** (read the
code, one inference), **speculative** (pattern suggests it, not proven).

Repo root for all paths: `/home/tradingapp/app`. Line numbers are as-read.

---

## Part A — Inconsistencies

| # | Severity | Category | Location | Finding | Evidence | Risk if unfixed |
|---|---|---|---|---|---|---|
| 1 | **P0** | Identifier / lock key | `backend/app/services/order_manager.py:987` | **certain** — The exposure lock keys the symbol raw and the account normalized, four lines apart in the same statement. `keys: list[object] = [exposure_key(intent, leg.symbol) for leg in intent.legs]` then `if intent.ibkr_account: keys.append(("__margin__", intent.ibkr_account.strip().upper()))`. `exposure_key` (`rms/models.py:116-120`) is `def exposure_key(intent: OrderIntent, symbol: str) -> str | tuple[int, str]:` / `if intent.account_id is not None: return (intent.account_id, symbol)` / `return symbol` — the `symbol` argument is passed through with no trim and no case fold. | `_exposure_guard`'s own docstring (`order_manager.py:974-985`) states the lock exists because "two strategies trading the same symbol on one account both read a pre-submit budget and both pass". Symbol casing comes straight from the payload (see #3). `"AAPL"` and `"aapl"` therefore take different `asyncio.Lock` objects out of `self._exposure_locks` (`order_manager.py:169`). | The mutual exclusion the guard was written to provide does not hold for case-variant symbols. Two concurrent intents both read a stale pre-submit exposure and both pass check 8 → per-symbol money cap bypassed → oversized position. |
| 2 | **P0** | Identifier / dedup barrier | `backend/app/rms/checks/money_per_stock.py:56` and `:68` | **certain** — Check 8 looks up both the limit and the running exposure on an un-normalized symbol: `account_limit = context.per_symbol_limits.get((intent.account_id, symbol))` and `existing_exposure = context.symbol_exposures.get(exposure_key(intent, symbol), Decimal(0))`, where `symbol` comes from `for leg in intent.legs: symbol_order_notionals[leg.symbol]` (`:43-44`). | Limits are written UPPER (`accounts/config_service.py:407` `sym = symbol.strip().upper()`, same at `:430`) and loaded raw into the context (`order_manager.py:249` `self._rms_context.per_symbol_limits[(limit.account_id, limit.symbol)]`). A lowercase inbound symbol misses the limit, falls through to `default_symbol_limits` (`:58`), and reads exposure `0`. | Silent per-symbol cap bypass. Worse than a hard failure because `:60-66` only rejects when *no* limit resolves at all; a default limit will resolve and quietly permit a larger position than configured. |
| 3 | **P0** | Identifier / root cause | `backend/app/services/model_blue/parser.py:140` | **certain** — Leg symbol is trimmed but never case-folded: `symbol = str(raw_leg.get("underlying") or bucket.get("underlying") or "").strip()`. In the same function, `action` gets `.strip().upper()` (`:42`) and `payload_side` gets `.upper()` (`:171`). | This is the sole entry point for Model Blue leg symbols. It flows `SignalLeg.symbol` → `model_blue/sizer.py:143,281` → `OrderLeg.symbol` (`model_blue/strategy.py:268`) → every consumer in #1, #2, #4, #5. | Upstream cause of findings 1, 2, 4, 5. Whatever casing TradingView emits becomes the key for the money cap, the exposure ledger, and the exposure lock. |
| 4 | **P1** | Identifier / key construction | `backend/app/services/order_manager.py:611` and `:618` | **certain** — Hydration builds the exposure key by hand instead of calling the helper: `key_a = (row.account_id, row.leg_a_symbol)`, `key_b = (row.account_id, row.leg_b_symbol)`, both raw from the DB row. | This is a fourth construction of the same key alongside `exposure_key` (`rms/models.py:116`), the read at `money_per_stock.py:68`, and the writes at `order_manager.py:1243`, `:1289`, `:1306`. The hand-built version always returns a tuple; `exposure_key` returns a bare `str` when `account_id is None`. | Startup-hydrated exposure lands in a different bucket from post-fill exposure whenever DB casing differs from payload casing, so the cap resets to zero across a restart. The divergent key *shape* is a latent second failure if `account_id` ever becomes optional. |
| 5 | **P1** | Identifier / dedup barrier | `backend/app/rms/models.py:102-106`, `backend/app/rms/checks/duplicate.py:27-29` | **certain** — The duplicate barrier keys on un-normalized `strategy_id` and `signal_id`, and the logic exists twice. Helper: `if intent.account_id is not None: return (intent.account_id, intent.strategy_id, intent.signal_id)`. Check 2 reimplements it inline rather than importing it: `lookup_key = (intent.strategy_id, intent.signal_id)` / `if intent.account_id is not None: lookup_key = (intent.account_id, intent.strategy_id, intent.signal_id)`. | `duplicate.py` imports `CheckResult, OrderAction, OrderIntent, RMSContext, RMSOutcome` from `app.rms.models` (`:4`) but not `duplicate_lookup_key`, which sits in the module it already imports from. The writer uses the helper (`order_manager.py:1283` `self._rms_context.processed_signals.add(duplicate_lookup_key(intent))`). | Two copies of a duplicate-order barrier that agree today by coincidence. Any change to one silently un-guards the other. Un-normalized `strategy_id` means a re-sent alert with different capitalization is not seen as a duplicate. |
| 6 | **P1** | Identifier / account key | `backend/app/services/position_reconciler.py:354`, `:141`, `backend/app/services/critical_recovery.py:348`, `backend/app/services/reconcile_service.py:67` | **certain** — Four sites build the broker→internal account map raw and look it up raw: `ibkr_to_account = {acc.ibkr_account: acc.id for acc in accounts}` then `account_id = ibkr_to_account.get(line.ibkr_account)` where `line.ibkr_account` is broker-supplied. Notably `position_reconciler.py:361-362` normalizes symbol *and* sec_type on the very next lines (`_norm_symbol(line.symbol)`, `_norm_sec_type(line.sec_type)`) but leaves the account key alone. | The same identifier is normalized at `order_manager.py:378` (`key = str(snapshot.ibkr_account).strip().upper()`), `services/account_margin.py:227,238`, `oms/ibkr_adapter.py:133,141,159`, `db/repositories/basket_repository.py:58`, `accounts/config_service.py:162,274`, `api/routes/baskets.py:30`, `api/routes/config.py:64,144`, `api/routes/margin.py:124,127`. | An account code that differs in case or padding between the `accounts` table and the IBKR `position` callback lands in `unmapped` and is classified `MISMATCH_UNMAPPED_ACCOUNT` (`position_reconciler.py:156`). Real broker positions become invisible to reconciliation. |
| 7 | **P2** | Identifier / authorization | `backend/app/api/routes/reconcile.py:43` and `:68`, `backend/app/services/reconcile_service.py:72` | **certain** — Account authorization compares raw: `user_account = current_user.account.ibkr_account if current_user.account else None`, and the service resolves the query parameter raw at `reconcile_service.py:72` `target_account_id = ibkr_to_account.get(ibkr_account)`. | `api/routes/config.py:64` performs the identical authorization comparison with both sides normalized: `if ibkr_account.strip().upper() != (user_acc_str or "").strip().upper():`. Two policies for one check. | Authorization and scoping that succeed or fail on capitalization. Direction of failure depends on stored casing, so it is either a spurious 404 or a scope leak. |
| 8 | **P2** | Identifier / naming | `backend/app/api/routes/orders.py:30`, `:50`, `:72` | **certain** — Three route handlers filter on `OMSOrder.account`, an attribute `OMSOrder` does not define: `orders = [o for o in orders if o.account == user_account]`, `if order.account != user_account:` (twice). | `OMSOrder` (`oms/models.py:118-148`) is a plain `@dataclass` whose fields are `internal_order_id, intent, symbol, side, quantity, ibkr_order_id, status, filled_quantity, remaining_quantity, average_fill_price, last_fill_price, limit_price, order_type, error_message, parent_signal_id, leg_index, basket_id, is_compensation, compensation_of_internal_order_id, commission, perm_id, executions, last_exec_id, resolved, pacer_delayed, timestamps, created_at`. The account lives on the nested intent: `oms/ibkr_adapter.py:241` `ib_order.account = order.intent.ibkr_account`. `grep -rn "def account" backend/app/oms/` returns nothing. | The concept "account" is spelled `intent.ibkr_account` in the OMS/adapter and `order.account` in the routes. Any `role == "user"` caller of these three endpoints raises `AttributeError` → 500. Non-admins cannot list, fetch, or cancel orders. |
| 9 | **P3** | Identifier / abandoned canon | `backend/app/core/identifiers.py:1-10` | **certain** — The module docstring claims total coverage — "Everything that derives a persisted key or a join column goes through here." — but the module only offers `normalize_strategy_id` and `normalize_trade_id`, and has exactly one importer. | `grep -rn "core.identifiers\|normalize_strategy_id\|normalize_trade_id" --include=*.py --exclude-dir=.venv backend/app/` → definitions in `core/identifiers.py` plus `services/worker_pool.py:35,38` only. No `normalize_symbol`, no `normalize_account`, despite ~70 inline `.strip().upper()` sites (see the normalization-gap list). | The designated canonical-normalization module covers two of the four identifier types and is imported by one file. Readers reasonably assume the docstring, then key on raw values as in #1–#7. |
| 10 | **P3** | Identifier / duplicated helper | `backend/app/services/position_reconciler.py:79-84`, `backend/app/services/critical_recovery.py:61-66` | **certain** — Byte-identical helper pairs in two modules: `def _norm_symbol(symbol: str) -> str: return symbol.strip().upper()` and `def _norm_sec_type(sec_type: str) -> str: return sec_type.strip().upper()`. | Both files also import from `app.db.models.account`, so there is no layering reason to avoid a shared helper. `core/identifiers.py` is the obvious home and does not have these. | Two copies of the normalization that #1–#7 are missing, in the two modules that happen to get it right. The correct behavior exists but is not reachable from the modules that need it. |
| 11 | **P1** | Error handling / three policies | `backend/app/broker/ibkr/tws_client.py:137-149` | **certain** — Three policies for "listener may not implement this callback", two of them inside one function. `hasattr` guard: `for listener in list(self._market_data_listeners): try: if hasattr(listener, "on_error"): listener.on_error(...) except Exception: logger.exception(...)`. Swallow: `for listener in list(self._listeners): try: listener.on_error(...) except AttributeError: pass except Exception: logger.exception(...)`. No guard: `tickPrice` (`:174-178`), `tickSize` (`:183-187`), `marketDataType` (`:192-196`), `connectionClosed` (`:158-162`). | `except AttributeError: pass` appears at `:146, :166, :219, :234, :247, :258, :271, :282, :295, :343, :354, :365, :376, :387` — 14 sites. The `hasattr` form appears at `:139` and `:204`. Partial protocols are real: `oms/ibkr_adapter.py` defines `on_order_status, on_open_order, on_exec_details, on_exec_details_end, on_commission_report, on_error, on_connection_closed` and `services/account_margin.py` defines `on_account_summary, on_account_summary_end, on_connection_closed, on_error` — neither defines `on_position`, `on_contract_details`, or `on_open_order_end`. So the swallow is load-bearing, but over-broad. | `except AttributeError: pass` cannot distinguish "listener lacks the method" from "an `AttributeError` was raised *inside* the method". At `:365` that method is `on_exec_details` — the fill path — and at `:330` it is `on_order_status` — the terminal-status path. An internal `AttributeError` there drops a fill or a terminal status with no log line at all. **Question for the team: is the swallow at the two execution-callback sites deliberate, or was it copy-propagated from the account/position callbacks where it is correct?** |
| 12 | **P2** | Error handling / two contracts, one condition | `backend/app/broker/ibkr/gateway_rate_limiter.py:169-184` vs `:218-222` | **certain** — Pacing exhaustion raises in the async path and returns `None` in the sync path. Async: `if remaining <= 0: with self._lock: self.metrics["timeout_count"] += 1` then `logger.warning("Gateway pacing timeout: ...")` then `raise GatewayPacingTimeout(...)`. Sync: `if remaining <= 0: with self._lock: self.metrics["timeout_count"] += 1` then `return None` — no log, no exception. | Same class, same condition, same metric increment, 40 lines apart. `try_acquire` (`:117-134`) is a third flavour that returns `None` immediately and increments a *different* metric, `try_acquire_denied`. | Every caller must know which of three acquire methods it holds and handle two different failure contracts. The sync path is additionally silent, so pacing starvation on TWS callback threads leaves no log trace — only the `timeout_count` metric moves. |
| 13 | **P2** | Error handling / retry policy divergence | six modules | **certain** — Six unrelated retry policies. (a) DB-configured fixed interval, no backoff, paper-ports-only: `oms/coordinator.py:690` `while attempt < policy.max_retries:` with `await asyncio.sleep(policy.retry_interval_sec)` (`:714`). (b) Module-constant fixed delay: `services/critical_recovery.py:171` `for attempt in range(1, MAX_RECOVERY_ATTEMPTS + 1):` with `await asyncio.sleep(RECOVERY_RETRY_DELAY_SEC)` (`:193`). (c) Exponential with two different caps in one loop: `services/watchdog/telegram.py:90` `await asyncio.sleep(min(2**attempt, 10))` for HTTP status vs `:97` `await asyncio.sleep(min(2**attempt, 5))` for exceptions. (d) DB attempt counter, no delay: `db/repositories/signal_repository.py:501` `SignalJobModel.attempt_count >= max_attempts`. (e) Unbounded fixed-delay reschedule: `services/pnl.py:118` `loop.call_later(0.05, callback)` with no attempt ceiling. (f) Sliding-window exhaustion: `services/watchdog/recovery_store.py:111` `def is_exhausted(self, service: str, max_attempts: int, window_seconds: int)`. | Grep for `backoff|max_retries|retry_count|attempt|retries|max_attempts` across `backend/app/` returns these six shapes and no shared helper. | Six policies to reason about during an incident, only one of which (c) backs off at all, and (e) retries forever. No single place to tighten pacing behaviour under broker stress. |
| 14 | **P2** | Error handling / halt vs continue | `backend/app/services/pnl.py:751-759` | **likely** — Failure to schedule a P&L persist is logged and abandoned: `asyncio.run_coroutine_threadsafe(self._schedule_persist(account_id, trade_id), loop)` wrapped in `except Exception: logger.exception("LivePnl persist schedule failed: ...")`. The computed `pnl` was already stored into `self._pending_pnl` at `:749` under the lock, and nothing re-drains `_pending_pnl` except a subsequent tick. | `_schedule_persist` is only reached from `_recompute` (`:750`) and from its own tail call (`:816`). If the scheduling raises, the pending value sits in the dict until the next tick for that trade arrives. | On a symbol that stops ticking (halt, entitlement loss, cooldown at `:274`/`:289`), `live_pnl` in Postgres silently freezes at its last persisted value while the dashboard shows it as current. Log-and-continue where the dashboard needs a staleness signal. |
| 15 | **P2** | DB / sync call in async path | `backend/app/api/routes/service_control.py:39`, `:55`, `:63`, `:82` | **certain** — `async def control_service(...)` calls blocking `subprocess.run` three times: `subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)`, `subprocess.run(["systemctl", "show", unit, ...], timeout=5)`, and `subprocess.run(cmd, capture_output=True, text=True, timeout=15)`. | `import subprocess` at `:7`; the handler is `async def` at `:39`. No `asyncio.to_thread`, unlike the four sites that do use it (`broker/ibkr/tws_client.py:449,499`, `instruments/cfd_discover.py:136`, `api/routes/webhooks.py:283`). | Up to 15 s of event-loop stall in the process that also runs the worker pool and the IBKR callbacks. An admin restarting `ibgateway` from the UI freezes fill handling for the duration. |
| 16 | **P2** | DB / sync call in async path | `backend/app/services/pnl.py:587-590` | **certain** — Blocking contract qualification on the event loop: `req_details = getattr(self._client, "request_contract_details", None)` / `if callable(req_details): details = req_details(contract, timeout=3.0)`. Reached from `_request_ticks` ← `watch_open` ← `order_manager.py:1109` (`self._live_pnl.watch_open(filled_intent)`) and ← `hydrate_from_position_rows` ← `order_manager.py:608`, both async. | The async wrapper already exists: `broker/ibkr/tws_client.py:445-449` `async def request_contract_details_async(...)` → `return await asyncio.to_thread(self.request_contract_details, contract, timeout=timeout)`. `instruments/cfd_discover.py:132-136` picks it up by `getattr` and falls back to `asyncio.to_thread` explicitly. `pnl.py` calls the sync one directly. | Three ways to make one call: the async wrapper, `to_thread` with a `getattr` probe, and a direct blocking call. Up to 3 s of event-loop stall *per leg* on the post-fill path. |
| 17 | **P2** | DB / session pattern deviation | `backend/app/api/routes/config.py:202`, `:251`, `:312`; `backend/app/api/routes/reconcile.py:76`; `backend/app/api/routes/emergency.py:116` | **certain** — Five route handlers open a *second* session while already holding the request-scoped one. Each does a function-body import then builds a factory: `from app.db.session import AsyncSessionLocal` / `session_factory = AsyncSessionLocal`, in handlers that already declare `session: AsyncSession = Depends(get_db_session)`. | Dominant pattern is one of two: services use `async with self._session_factory() as session, session.begin():` (40 occurrences across 15 files — `kill_switch.py` 7, `order_manager.py` 7, `worker_pool.py` 5, `critical_recovery.py` 4, `coordinator.py` 4, `recovery.py` 3, and 9 others with 1–2 each); routes use `Depends(get_db_session)` plus explicit `await session.commit()` (12 in `config.py`). Only `order_manager.py` mixes both inside one module (7 × `session.begin()`, 2 × `.commit()`). | Two independent transactions per logical request, so a partially-applied operation is possible where the code reads as atomic. The function-body import is what hides it from a reader scanning the module header. |
| 18 | **P3** | DB / transaction boundary | `backend/app/db/session.py:52-55` | **certain** — The request dependency neither opens a transaction nor commits or rolls back: `async def get_db_session() -> AsyncGenerator[AsyncSession, Any]: async with AsyncSessionLocal() as session: yield session`. Every write route must remember `await session.commit()` itself. | 12 explicit commits in `api/routes/config.py` (`:345, :404, :474, :530, :577, :613, :646, :702, :729, :762, :805, :847`). The service-side factory pattern gets this for free via `session.begin()`. `expire_on_commit=False` and `autoflush=False` are set globally at `session.py:47-48`, so this part is at least consistent. | A write route that omits the commit discards silently — no error, no log. The service layer cannot make this mistake; the route layer can, and does so 12 times correctly by hand. |
| 19 | **P1** | State authority / ledger vs IBKR | `backend/app/services/model_blue/persistence.py:33-34`, `:82-83` | **certain** — Entry and exit marks — the inputs to realized P&L — read the in-memory execution dict and then fall back to an IBKR-reported aggregate: `derived = executions_weighted_average(getattr(order, "executions", {}) or {})` / `raw = derived or order.average_fill_price or order.last_fill_price`. `order.average_fill_price` is populated from the broker's `orderStatus` callback (`oms/ibkr_adapter.py:601-607` region, `avgFillPrice`). | The durable fill ledger has its own aggregator, `weighted_average_price(rows: Sequence[ExecutionModel])` at `db/repositories/execution_repository.py:14`, and it is **never called in production**: `grep -rn "\bweighted_average_price\b" --include=*.py --exclude-dir=.venv backend/ scripts/` → `backend/tests/test_execution_audit_persistence.py:24,271` and the definition. The `executions` table is written (`ExecutionRepository.upsert`, `:76`) but never read back for pricing. | Given the stated ledger-as-truth intent, this is the design violation: the ledger is write-only for P&L purposes, and the authority is an in-process dict with an IBKR aggregate as fallback. A restart between fill and persist loses `order.executions` and silently drops to the broker's number. |
| 20 | **P3** | State authority / recomputation | `backend/app/services/model_blue/persistence.py:216` and `:248` | **certain** — `_exit_marks_from_orders(orders)` is computed twice per close, the second time only to build a log line: passed to `close_trade` at `:216` as `exit_marks=_exit_marks_from_orders(orders)`, then recomputed at `:248` inside `logger.info(... {k: str(v) for k, v in _exit_marks_from_orders(orders).items()})`. | Both calls are in `persist_close`, inside the same `async with ... session.begin():` block opened at `:212`. | Two evaluations of the P&L input in one transaction. Harmless today because the function is pure over a fixed list, but the log now claims to show what was written without being the same value. |
| 21 | **P3** | Layering / private reach-through | `backend/app/services/pnl.py:174-178` | **certain** — A repository is instantiated without `__init__` so a method can be borrowed as a free function: `from app.db.repositories.position_repository import PositionRepository` / `helper = PositionRepository.__new__(PositionRepository)` / `for row in rows: trade = helper.to_open_trade(row)`. | `PositionRepository` is already imported at module scope (`pnl.py:14`), so the function-body import at `:174` is redundant with it. `to_open_trade` is used as a pure row→domain mapper, which is what it is at `db/repositories/trade_repository.py:56` too (`return self._positions.to_open_trade(row)`). | `__new__` bypasses `__init__`, so the object has no `_session`. It works only as long as `to_open_trade` never touches `self._session`. A future maintainer adding a lazy load to `to_open_trade` breaks P&L hydration with an `AttributeError` at startup. |
| 22 | **P2** | Config / same value twice | `backend/app/core/config.py:51-55` vs `backend/app/broker/ibkr/gateway_rate_limiter.py:17-21` | **certain** — All five pacing values are defined in two places with identical numbers. Settings: `ibkr_gateway_max_msg_per_sec = 30.0`, `ibkr_gateway_normal_msg_per_sec = 24.0`, `ibkr_gateway_emergency_reserve_per_sec = 6.0`, `ibkr_gateway_max_wait_sec = 8.0`, `ibkr_gateway_error100_cooldown_sec = 2.0`. Module constants: `DEFAULT_MAX_MSG_PER_SEC = 30.0`, `DEFAULT_NORMAL_MSG_PER_SEC = 24.0`, `DEFAULT_EMERGENCY_RESERVE_PER_SEC = 6.0`, `DEFAULT_MAX_WAIT_SEC = 8.0`, `DEFAULT_ERROR100_COOLDOWN_SEC = 2.0`. | Production always passes the settings values explicitly (`main.py:47-51`), so the module constants are only reachable as parameter defaults — exercised by tests that omit kwargs (`tests/test_gateway_rate_limiter.py:20`, `tests/test_basket_retry.py:206`, `tests/test_ibkr_adapter_pacing.py:67,97,123`). | Operationally significant pacing limits with two sources of truth. Raising the env var leaves the module constants stale, and the tests keep passing against the old numbers, so the divergence is invisible in CI. |
| 23 | **P2** | Config / env read at call depth | `backend/app/db/session.py:21`, `backend/app/api/deps.py:67`, `backend/app/main.py:82` | **certain** — `TRADINGAPP_TESTING` is read straight from the environment at three call depths plus once inside the settings factory, and is not a `Settings` field. All four are the same expression: `os.environ.get("TRADINGAPP_TESTING") == "1"` (`session.py:21`, `deps.py:67`, `main.py:82`, `core/config.py:142`). | `grep -rn "os\.environ\|os\.getenv" --include=*.py --exclude-dir=.venv backend/app/ | grep -v "core/config.py"` returns exactly those three lines. Every other configuration value goes through `Settings`. | A flag that changes the engine's pool class (`session.py:22-28`, `NullPool`), a dependency's behaviour, and startup wiring, but is invisible in the settings object an operator would inspect. It cannot be overridden per-process the way the rest of the config can. |
| 24 | **P2** | Config / inline magic numbers | `backend/app/services/pnl.py` | **certain** — Operationally significant constants inline in the market-data path: `self._next_req = 50000` (`:92`), `loop.call_later(0.05, callback)` (`:118`), `self._cooldowns[c_key] = time.time() + 600.0  # 10 minute backoff` (`:274` and again `:289`), `STALE_THRESHOLD_SEC = 15.0` declared as a *local variable inside a method* (`:474`), `details = req_details(contract, timeout=3.0)` (`:590`), `_PERSIST_MIN_INTERVAL_SEC = 1.0` (`:37`, the only one promoted to module scope). | `core/config.py` has no corresponding fields; the file's only imported knob is `PRIORITY_MARKET_DATA` (`pnl.py:12`). The `0.05` value also appears as the sleep quantum at `gateway_rate_limiter.py:186` and `:224`. | Market-data entitlement backoff (10 min), staleness threshold (15 s), and contract-qualification timeout (3 s) cannot be tuned without a code change and redeploy. The 10-minute cooldown is duplicated, so a fix to one leaves the other. |
| 25 | **P3** | Config / retry ceiling in five places | five modules | **certain** — The retry ceiling `3` is independently defined five times: `oms/retry_policy.py:21` `max_retries: int = 3`, `db/models/execution_settings.py:17` `max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)`, `db/models/signal.py:81` `max_attempts: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="3")`, `db/repositories/signal_repository.py:486` `async def reclaim_stale_jobs(self, max_attempts: int = 3)`, `accounts/config_service.py:457` `max_retries=3`. Plus `services/watchdog/telegram.py:20` `max_retries: int = 3`. | Two of these are also two different *names* for a retry ceiling — `max_retries` and `max_attempts` — see #27. | Six defaults to keep in step. The DB `server_default` and the Python `default` at `execution_settings.py:17` can already disagree if a migration sets one without the other. |
| 26 | **P3** | Naming / one concept, two names | `backend/app/oms/models.py:96,109` vs `backend/app/db/repositories/execution_repository.py:14,30` | **certain** — Fill aggregation exists under two name pairs over two row types. In-memory: `executions_weighted_average(executions: dict[str, BrokerExecution])`, `executions_commission_total(executions: dict[str, BrokerExecution])`. Ledger: `weighted_average_price(rows: Sequence[ExecutionModel])`, `total_commission(rows: Sequence[ExecutionModel])`. The algorithms are the same — accumulate `qty`/`notional`, skip `q <= 0`, return `None` on zero quantity. | The in-memory pair has five production callers (`order_manager.py:1190`, `model_blue/persistence.py:33,82,47`, `oms/ibkr_adapter.py:657,862,863,937`, `db/repositories/order_repository.py:89`). The ledger pair has none (see #19 evidence). | Two vocabularies for "average fill price" split by which representation you happen to hold. The authoritative-sounding name (`weighted_average_price`, on the ledger table) is the dead one. |
| 27 | **P3** | Naming / one concept, two names | `backend/app/db/models/signal.py:81` vs `backend/app/db/models/execution_settings.py:17` | **certain** — The retry ceiling is `max_attempts` on `signal_jobs` and `max_retries` on `execution_settings`, and both appear in the same reasoning paths: `recovery.py:116` `if job.attempt_count >= job.max_attempts:` against `coordinator.py:690` `while attempt < policy.max_retries:`. | Off-by-one semantics differ with the names: `max_attempts` counts the first try, `max_retries` counts retries after it. `retry_policy.py:28-29` validates `if self.max_retries < 0: raise ValueError("max_retries must be >= 0.")` — so `max_retries=0` means one attempt, while `max_attempts=0` would mean none. | Two names with two off-by-one conventions for one concept, both defaulting to `3`, meaning 3 and 4 total attempts respectively. This is exactly the kind of divergence that produces the bugs the other sessions are chasing. |
| 28 | **P3** | Naming / one name, two meanings | `backend/app/services/pnl.py:151` | **certain** — `trade_id` here is derived from `signal_id` by string surgery: `key = (intent.account_id, intent.signal_id.split(":CLOSE")[0].split(":UNWIND:")[0])`, and that value is then passed as the `trade_id` parameter throughout (`_request_ticks(self, account_id: int, trade_id: str, leg)` at `:511`, `update_live_pnl(account_id=account_id, trade_id=trade_id, ...)` at `:820-821`). | Elsewhere `trade_id` is a first-class parsed field: `model_blue/parser.py:48` `trade_id = str(payload.get("trade_id") or "").strip()`, and `core/identifiers.py:21` provides `normalize_trade_id` for it. The `:CLOSE` suffix convention is minted in two other places — `worker_pool.py:42` `signal_id = f"{trade_id}:CLOSE"` and `model_blue/persistence.py:220` `persist_signal_id=f"{trade_id}:CLOSE"` — and `:UNWIND:` in a third. | `trade_id` means "the parsed TradingView token" in the parser and "signal_id with known suffixes stripped" in the P&L service. Adding a fourth suffix elsewhere silently mis-keys `live_pnl` rows, because the strip list here is hardcoded. |

---

## Part B — Bloat

| # | Severity | Category | Location | Finding | Evidence | Risk if unfixed |
|---|---|---|---|---|---|---|
| 29 | **P3** | Dead code | 21 symbols across 15 files | **certain** — Twenty-one definitions have no reference anywhere in the repo other than their own definition line. Full list with grep output in **Deletion candidates** below. | An AST walk over `backend/app/**/*.py` collected every `FunctionDef`/`AsyncFunctionDef`/`ClassDef`, then counted `\bname\b` across every `.py`, `.ts`, `.tsx`, `.json`, `.sh`, `.service`, `.timer`, `.md`, `.cfg`, `.ini`, `.toml`, `.yaml`, `.yml` file under `backend/`, `scripts/`, `deploy/`, `frontend/src`. Word-boundary matching also catches string-based dispatch (`getattr(obj, "name")`). FastAPI route handlers were excluded by hand since the decorator registers them. Each survivor was then re-confirmed with an explicit `grep -rn`. | Twenty-one symbols that a reader must evaluate and dismiss, including two repository query methods and one dead `Settings` property that the docs still describe. |
| 30 | **P2** | Duplicated logic | `backend/app/broker/ibkr/gateway_rate_limiter.py:136-190` vs `:192-228` | **certain** — `acquire` and `blocking_acquire` are the same 30-line loop twice. Both compute `deadline = time.monotonic() + max_wait`, initialize `total_waited`/`delayed`, loop on `self._try_consume_locked(priority)` under `self._lock`, call `_record_acquire_locked`, fall through to `wait_sec = self._seconds_until_available_locked(priority)`, check `remaining = deadline - time.monotonic()`, and sleep `min(wait_sec, remaining, 0.05)`. **Diff in prose:** the async version hardcodes `self.max_wait_sec` while the sync version accepts a `timeout` override; the async version logs `"IBKR submit paced: ..."` on a delayed grant (`:152-159`) and `"Gateway pacing timeout: ..."` on expiry (`:172-180`), the sync version logs nothing in either case; the async version raises `GatewayPacingTimeout`, the sync version returns `None`; sleeps are `await asyncio.sleep` vs `time.sleep`. **The async version is authoritative** — it is the one with observability and an explicit failure signal. The sync version is the stale copy. | Both are live: `pnl.py:125` uses `try_acquire`, and `blocking_acquire` exists for "TWS callback / worker threads" per its docstring (`:199`). | Two implementations of the pacing loop that must be kept numerically identical, plus the divergent error contract in #12. A change to the token math has to be made twice. |
| 31 | **P3** | Duplicated logic | `backend/app/services/pnl.py:645-672` vs `:695-719`; `:313-321` vs `:335-343` | **certain** — Four copies of "send `reqMktData`, clean up the four dicts on failure, log". The `_send` closure inside `_request_ticks` (`:645-672`) and the `_send` closure inside `_issue_request_ticks` (`:695-719`) both do `req_type(1)`, `req_mkt(req_id, contract, "221", False, False, [])`, and on exception pop `self._by_req`, `self._contract_reqs`, `self._req_to_contract`, `self._listeners_by_req` then `logger.exception("LivePnl reqMktData failed: ...")`. **Diff in prose:** the first logs `secType` and `conId` in its success line, the second omits both. The first is authoritative (richer log); the second is the retry path and is stale. Similarly `_resubscribe_all_active`'s inner `_send` (`:313-321`) duplicates `_resubscribe_one`'s `_send` (`:335-343`) — and `_resubscribe_all_active` already delegates its own retry to `_resubscribe_one` (`:324`), so the inline copy exists only to serve the first attempt. | `_issue_request_ticks` is reached only from `_request_ticks`'s `_retry` closure (`:675`); `_resubscribe_one` only from `:324` and its own `_retry` (`:346`). | Retrying a market-data subscription takes a different code path from requesting it, with a quieter log. A fix to the cleanup set has to be applied in two places, and the retry path is the one that gets forgotten. |
| 32 | **P3** | Abandoned abstraction | `backend/app/db/repositories/trade_repository.py:15-56` | **certain** — `TradeRepository` forwards every method to `PositionRepository` and adds no behaviour. `__init__` builds `self._positions = PositionRepository(session)` (`:20`); `get_open` → `return await self._positions.get_open_trade(...)`; `get_row` → `return await self._positions.get_by_trade_id(...)`; `open_trade` → `return await self._positions.open_trade(...)` passing all five arguments through unchanged; `close_trade` → `self._positions.close_trade(...)` then `return self._positions.to_open_trade(row)`. Only `close_trade` adds anything, and what it adds is one call to a public method of the wrapped object. | Two consumers: `services/model_blue/db_trade_book.py:38,59,72` and `services/model_blue/persistence.py:163,213`. `get_row` has zero consumers (see deletion candidates). Meanwhile `services/pnl.py:176` reaches around the wrapper entirely with `PositionRepository.__new__(PositionRepository)` to get at `to_open_trade` (#21). | A pass-through layer between Model Blue and the position store, which one caller already bypasses. Readers must open two files to find out what a trade write does. |
| 33 | **P3** | Speculative generality | `backend/app/oms/models.py:25` and `:172` | **certain** — Two aliases introduced as system-wide vocabulary, one unused in application code and one unused everywhere. `# Clean alias for system-wide order status` / `OrderStatus = OMSOrderStatus` and `# Clean alias for system-wide order` / `Order = OMSOrder`. | `grep -rn "Order = OMSOrder" --include=*.py backend/` → the definition only. For `OrderStatus`: the one production consumer does *not* use the alias — `schemas/api_schemas.py:8` is `from app.oms.models import OMSOrderStatus as OrderStatus`, aliasing the canonical name locally. The only importer of the alias is `tests/test_order_manager.py:9`. Every other site in the codebase (60+ references across `oms/ibkr_adapter.py`, `oms/coordinator.py`, `oms/oms_service.py`, `services/*`) uses `OMSOrderStatus` directly. | Two names per class, with the "clean" ones losing. The comments assert a convention that the codebase rejected. |
| 34 | **P3** | Speculative generality | `backend/app/broker/ibkr/gateway_rate_limiter.py:123` and `:130` | **certain** — A variable initialized to `False`, never reassigned, then reported as a result: `delayed = False` at `:123`, and `try_acquire` returns `AcquireResult(delayed=delayed, ...)` at `:130`. Nothing between them touches it. | `try_acquire` is non-blocking by construction — its docstring is "Non-blocking acquire. Returns None if no token is available now." — so `delayed` can only ever be `False`. Contrast `acquire` (`:144`) and `blocking_acquire` (`:203`), where the same variable is genuinely set to `True` at `:188`/`:226`. | A field on the result type that is structurally constant for one of three producers. Callers branching on `result.delayed` get a silent always-false from `try_acquire`. |
| 35 | **P3** | Patch residue | `backend/app/services/pnl.py` | **certain** — Six function-body re-imports of modules already imported at file scope. `import asyncio` at `:3` then again at `:143`; `import time` at `:5` then again at `:239` and `:617`; `from app.db.repositories.position_repository import PositionRepository` at `:14` then again at `:174`; `import datetime` inside three separate methods (`:382`, `:472`, and via `:240`). | Module-scope imports are at `pnl.py:3-15`. The re-imports are inside `watch_open`, `on_error`, `_request_ticks`, `hydrate_from_position_rows`, `on_tick_price`, and `get_market_data_health`. | Residue from rounds of patching this file in isolation. Harmless at runtime, but it disguises the module's real dependency set — which is how the redundant `PositionRepository` import at `:174` came to sit next to the `__new__` hack in #21. |
| 36 | **P3** | Dead dependency-ish constant | `backend/app/broker/ibkr/gateway_rate_limiter.py:15` | **certain** — A documented ceiling that nothing reads: `# Documented IBKR Error 100 limit: 50 msg/sec` / `IBKR_DOCUMENTED_CEILING_MSG_PER_SEC = 50.0`. | `grep -rn "IBKR_DOCUMENTED_CEILING_MSG_PER_SEC" --include=*.py --exclude-dir=.venv backend/` → `backend/app/broker/ibkr/gateway_rate_limiter.py:15` only. Nothing validates `max_msg_per_sec` against it; the constructor's checks (`:63-74`) never mention it. | Reads as an enforced safety ceiling. It is a comment with a float attached. An operator raising `IBKR_GATEWAY_MAX_MSG_PER_SEC` above 50 gets no warning. |
| 37 | **P3** | Speculative generality | `backend/app/rms/models.py:130-131` | **likely** — An alias created for a migration that appears to have completed: `# Generic aliases: execution pipeline operates on List[TradeLeg], not leg_a/leg_b.` / `TradeLeg = OrderLeg`. | It is re-exported from the package (`rms/__init__.py` imports `TradeLeg` and lists it in `__all__`), which is why it does not show up as unreferenced. But the execution pipeline the comment describes uses `OrderLeg` directly: all ten `OrderLeg(` construction sites (`position_close_service.py:100,115`, `kill_switch.py:365,381`, `broker_flatten_service.py:154`, `order_manager.py:886`, `model_blue/strategy.py:234,267`, `pnl.py:182`, `coordinator.py:896`) use the real name. | The comment argues for a naming convention that the code does not follow, and the `__all__` export keeps it alive. **Question: is `TradeLeg` part of a published surface that external tooling imports, or is it leftover from the leg_a/leg_b migration?** |
| 38 | **P2** | Speculative generality / flags permanently off | `backend/app/core/config.py:65`, `:68`, `:72` | **certain** — Three subsystems are dark by default: `market_value_check_enabled: bool = False`, `margin_whatif_enabled: bool = False`, `margin_scan_enabled: bool = False`. The market-value one is the costly case: `rms/checks/model_market_value.py:46` `if not context.market_value_check_enabled:` returns PASS immediately, yet the state that feeds it is maintained unconditionally on every intent. | `order_manager.py:153` wires the flag onto the context. The bookkeeping runs regardless: `_reseed_model_value_used` (`:342-373`), the `model_value_used` increments at `:1252-1263`, `:1294-1299`, and `:1311-1318`, and — most significantly — `_exposure_guard` acquires an extra lock for it every time (`:992-994` `value_key = model_value_key(intent)` / `keys.append(("__model_value__", *value_key))`). `margin_whatif_enabled` gates `oms/ibkr_adapter.py:322`, which is what makes `_fail_whatif` (deletion candidate #2) unreachable. `margin_scan_enabled` gates all of `margin_scanner.py` plus two startup hooks (`main.py:149`, `:168`). | Every intent pays a lock acquisition and three dict updates to feed a check that returns PASS unconditionally in the default deployment. The `market_value_utilisation_cap` setting (`config.py:64`, defaulted to `1.0` with the comment "Cap defaults to 1.0 because pair_max_allocation_pct is the finer-grained control") is therefore inert twice over. Reviewers of the concurrency work will see a lock whose purpose is unverifiable at runtime. |
| 39 | **P2** | Patch residue / flag permanently on | `backend/app/instruments/execution_override.py:1-8`, `backend/app/core/config.py:104-107` | **certain** — A module that declares itself temporary is on by default in a live-money system. Module docstring: `"""TEMPORARY paper/demo execution mapping.` ... `Disable with PAPER_EXECUTE_STK_AS_CFD=false. Do not copy this mapping into the IBKR adapter, TWS client, OMS placeOrder, basket, or RMS."""`. Setting: `# TEMPORARY paper/client-demo: requested STK executes as IBKR CFD.` / `# Raw TradingView / persisted signal instrument_type stays STK.` / `# Disable with PAPER_EXECUTE_STK_AS_CFD=false.` / `paper_execute_stk_as_cfd: bool = True`. | Three production call sites, all via function-body imports: `order_manager.py:1462`, `:1516`, `:1543` (`from app.instruments.execution_override import execution_instrument_type`), plus `instruments/resolver.py:157`. Contrast the other three flags in #38, all defaulted `False`. | The only feature flag in the file that defaults *on* is the one labelled temporary and demo-only, and it changes which instrument actually gets executed (STK → CFD) while the persisted signal keeps saying STK. That divergence between requested and executed type is deliberate and documented, so not filed as a defect — but "TEMPORARY" plus `= True` plus live money is worth an explicit decision. **Question: is CFD execution now the intended permanent production mapping? If so the naming (`paper_`, `_DEMO`, "TEMPORARY") is actively misleading during incident response.** |
| 40 | **P3** | Speculative generality / unused parameter | `backend/app/instruments/execution_override.py:25` and `:34` | **certain** — A keyword-only override parameter that no caller ever passes: `def execution_instrument_type(requested: str | None, *, enabled: bool | None = None)` with `on = paper_execute_stk_as_cfd_enabled() if enabled is None else enabled`. | `grep -rn "execution_instrument_type\|paper_execute_stk_as_cfd_enabled" --include=*.py --exclude-dir=.venv backend/` → the three `order_manager.py` call sites (`:1462, :1516, :1543`), `resolver.py:25,157`, and the definitions. None passes `enabled=`. The tests exercise the *sibling* seam instead, `resolve_leg(..., apply_demo_override=...)` (`tests/test_stk_to_cfd_demo_override.py:71,99,170,180,191,199`), which is genuinely used in production too (`pnl.py:558` `apply_demo_override=False`). | Two injection seams for one flag, one live and one never exercised. The dead branch of `:34` is untested code in the path that decides whether an order is placed as STK or CFD. |
| 41 | **P3** | Patch residue / TODO for live code | `backend/app/api/routes/webhooks.py:30` and `:281` | **certain** — Two "remove later" markers on a code path that is still running in the live ingest handler: `# TEMPORARY: append-only CSV of every accepted webhook. Remove later.` (`:30`) and `# TEMPORARY: also dump every accepted signal to CSV. Remove later.` (`:281`), the latter immediately above `await asyncio.to_thread(_append_incoming_signal_csv, _incoming_signal_csv_row(...))` (`:283-292`). | These are two of only four `TEMPORARY`/`TODO`-class markers in the entire application tree: `grep -rniE "#\s*(todo|fixme|temporary|xxx|hack|deprecated|remove later|for now)\b" --include=*.py --exclude-dir=.venv backend/app/` returns `webhooks.py:30`, `webhooks.py:281`, `core/config.py:97`, `core/config.py:104`. The JSON sibling of this CSV dump is already dead (`_save_raw_capture_file`, deletion candidate #3). | Unbounded append-only file write on the hot webhook path, marked for removal, with its JSON counterpart already silently retired. Half of a capture mechanism was removed and the marker on the other half was left. |
| 42 | **P3** | Patch residue | `backend/app/oms/retry_policy.py:7-12` | **speculative** — A live-trading guard expressed as a port allowlist: `PAPER_IBKR_PORTS = frozenset({7497, 4002})` and `def paper_retry_ports_allowed(ibkr_port: int) -> bool: """Retries are demo/paper Gateway/TWS ports only (not 7496/4001 live)."""` / `return int(ibkr_port) in PAPER_IBKR_PORTS`. The module docstring is "Paper-only basket retry / square-off timing. Does not submit orders." | Enforced at `oms/coordinator.py:629-633`: `if policy is None or not policy.enabled or policy.max_retries <= 0:` ... `logger.warning("AUTO-SQUARE-OFF retries skipped: paper ports only (IBKR live port or flag off)")`. The default port in `core/config.py:46` is `ibkr_port: Annotated[int, Gt(0)] = 7497` — a paper port. | On a live gateway (7496/4001) the entire `ExecutionRetryPolicy` — `enabled`, `square_off_after_sec`, `max_retries`, `retry_interval_sec`, `retry_window_sec`, all operator-editable via `api/routes/config.py:739`, all validated at `retry_policy.py:25-35` — is inert. The dashboard presents live knobs that do nothing in production. **Question: is the port gate the intended permanent safety policy for live money, or a paper-testing scaffold that should now be a config flag?** If intended, the dashboard should say so; filing as consistency, not defect. |

| 43 | **P3** | Dependencies | `backend/pyproject.toml:14`, `:21` | **certain** — Two declared dependencies with no importer. `"passlib[bcrypt]>=1.7.4"` appears nowhere in the repo outside its own declaration; password hashing calls `bcrypt` directly (`app/core/security.py:8` `import bcrypt`), and `bcrypt` is separately declared at `:22`. `"python-dotenv>=1.2.2"` likewise has no importer; `.env` loading is pydantic-settings' own, via `SettingsConfigDict(env_file=".env", ...)` at `app/core/config.py:21`. | `grep -rn "passlib" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=.mypy_cache .` → `./backend/pyproject.toml:21` only. Same command for `dotenv` → `pyproject.toml:14` plus `uv.lock` entries, one of which (`uv.lock:751`) shows it arriving transitively via pydantic-settings anyway. For contrast, `email-validator` (`:23`) has no direct import either but *is* genuinely required, by `EmailStr` at `app/api/routes/auth.py:24`. | `passlib` is the notable one: it is a password-hashing library declared but unused, sitting next to a hand-rolled `bcrypt` call, in a system with JWT auth. It is also unmaintained upstream, so it will eventually break the build for no benefit. `python-dotenv` is only a redundant direct pin of a transitive dep. |

---

## Scope notes

Three of the requested categories came back clean or near-clean, which is worth
recording so the next reviewer does not re-derive it:

- **Bare `except:`** — none. `grep -rn "except\s*:" --include=*.py --exclude-dir=.venv
  backend/app/` returns nothing. Every handler names at least `Exception`. The
  error-handling problems in #11–#14 are about *which* exception is swallowed and what
  happens next, not about bare catches.
- **Imported but unused** — none. `ruff check --select F401,F811,F841 app/` reports
  exactly two findings, both unused *locals*, neither an import:
  `app/services/watchdog/health.py:159` (`xvfb_detail = None`) and
  `app/services/watchdog/notifier.py:295` (`underlying = health.underlying_error`).
  Both are in the watchdog, both **P3**.
- **Commented-out code blocks** — none found. The `TEMPORARY`/`TODO`-class markers are
  the four listed in #41, and all four annotate live code rather than commented-out
  code.

One dependency worth a note rather than a finding: `redis>=5.2.1` (`pyproject.toml:17`)
is real but is not a trading-path dependency. Its importers are the separate demo app
(`demo_streaming/stream.py:7-8`, `demo_streaming/api.py:18`, `demo_streaming/main.py:9`)
and two monitoring readers (`app/services/system_monitor_service.py:18`,
`app/services/watchdog/health.py:697`). Nothing in the signal, RMS, OMS, or persistence
path touches it, which matches the correction already recorded in `MAP.md`.

**Not covered in this session:** the 24 files in `backend/alembic/versions/` were not
audited for superseded or unreferenced migrations, and the `frontend/` tree was searched
only as a reference target for backend symbols, not reviewed for its own bloat.
`services/watchdog/` was searched for the cross-cutting patterns above and contributed
seven deletion candidates, but its internal consistency was not reviewed in depth.

---

## Deletion candidates

Each entry is the actual command and its output. Runs are from `/home/tradingapp/app`.
`.mypy_cache`, `__pycache__`, and `.pytest_cache` hits are compiled/cached artifacts of
the definition itself and are noted but not treated as references.

**Method note.** These were found by AST-collecting every top-level and method
definition under `backend/app/`, then word-boundary counting each name across
`backend/`, `scripts/`, `deploy/`, and `frontend/src` for extensions `.py .ts .tsx
.json .sh .service .timer .md .cfg .ini .toml .yaml .yml`. Word-boundary matching
catches string-based dispatch such as `getattr(obj, "name")` and task-registry
lookups. FastAPI route handlers were excluded manually because the decorator, not a
caller, registers them.

### 1. `HeadroomView` — `backend/app/rms/margin_estimate.py:163`

```
$ grep -rn "HeadroomView" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.3.db: binary file matches
./backend/app/rms/margin_estimate.py:163:class HeadroomView:
grep: ./backend/app/rms/__pycache__/margin_estimate.cpython-312.pyc: binary file matches
```

Class definition only. No constructor call, no type annotation use, no test.

### 2. `_fail_whatif` — `backend/app/oms/ibkr_adapter.py:301`

```
$ grep -rn "_fail_whatif" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.1.db: binary file matches
grep: ./backend/app/oms/__pycache__/ibkr_adapter.cpython-312.pyc: binary file matches
./backend/app/oms/ibkr_adapter.py:301:    def _fail_whatif(self, order_id: int, exc: BaseException) -> None:
```

Method definition only — not even called from within its own class. Note this sits in
the what-if margin probe path, which is itself gated off by default
(`core/config.py:68` `margin_whatif_enabled: bool = False`).

### 3. `_save_raw_capture_file` — `backend/app/api/routes/webhooks.py:59`

```
$ grep -rn "_save_raw_capture_file" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.12.db: binary file matches
./backend/app/api/routes/webhooks.py:59:def _save_raw_capture_file(capture_data: dict[str, Any], filename: str) -> None:
grep: ./backend/app/api/routes/__pycache__/webhooks.cpython-312.pyc: binary file matches
./docs/archive/COMPLETE_TRADING_SYSTEM_DOCUMENTATION.md:2363:  5. Writes disk capture: `data/tradingview_webhooks/{timestamp}.json` via `_save_raw_capture_file` (off-loop helper) and appends CSV row to `data/tradingview_webhooks/incoming_signals.csv` via `_append_incoming_signal_csv` (thread-locked, `asyncio.to_thread`).
./docs/archive/COMPLETE_TRADING_SYSTEM_DOCUMENTATION.md:3519:## 30. `_save_raw_capture_file / _append_incoming_signal_csv` — `backend/app/api/routes/webhooks.py:59,127`
```

Definition only — no caller. The archived documentation describes it as step 5 of the
webhook ingest path, writing `data/tradingview_webhooks/{timestamp}.json`. The
neighbouring helper it is documented alongside, `_append_incoming_signal_csv`, *is*
still live (`webhooks.py:283` `await asyncio.to_thread(`). So raw JSON payload capture
was dropped while CSV capture was kept. **Question: was disk JSON capture deliberately
retired in favour of the `signals_inbound` table, or did it regress?** If deliberate,
delete the function and the doc section; if not, raw payload capture is silently off.

### 4. `as_catalog` — `backend/app/db/repositories/instrument_repository.py:119`

```
$ grep -rn "as_catalog" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.12.db: binary file matches
grep: ./backend/app/db/repositories/__pycache__/instrument_repository.cpython-312.pyc: binary file matches
./backend/app/db/repositories/instrument_repository.py:119:def as_catalog(catalog: InstrumentCatalog) -> InstrumentCatalog:
```

Module-level function, definition only.

### 5. `candle_timeframe_minutes` — `backend/app/core/config.py:120`

```
$ grep -rn "candle_timeframe_minutes" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.14.db: binary file matches
grep: ./backend/app/core/__pycache__/config.cpython-312.pyc: binary file matches
./backend/app/core/config.py:120:    def candle_timeframe_minutes(self) -> int:
./docs/archive/COMPLETE_TRADING_SYSTEM_DOCUMENTATION.md:4344:| `candle_timeframe` | `CANDLE_TIMEFRAME` | Candle string parsed by `candle_timeframe_minutes` | `backend/app/core/config.py:78` property only; **not** used by Model Blue webhook path | `"5 mins"` | No | Low | `CANDLE_TIMEFRAME="5 mins"` |
./docs/archive/COMPLETE_TRADING_SYSTEM_DOCUMENTATION.md:4375:- `candle_timeframe_minutes` (`backend/app/core/config.py:77`) — parses `candle_timeframe` to int minutes (`"5 mins"` → `5`; `"15 mins"` → `15`; invalid → `5`).
./docs/backend-config.md:48:| `candle_timeframe` | `CANDLE_TIMEFRAME` | `"5 mins"` | Parsed by `candle_timeframe_minutes`; **not** used by Model Blue webhook path |
./docs/backend-config.md:58:Property: `candle_timeframe_minutes` — parses `candle_timeframe` to int minutes (defaults to 5).
```

No code reference. The documentation itself twice records that it is unused by the
Model Blue webhook path. Deleting it should also drop the `candle_timeframe` setting
(`config.py:93`) and the two doc rows. Same applies to the neighbouring
`trading_symbol`, `strategy_candle_count`, and `order_quantity` settings
(`config.py:92-95`) — **speculative**, not yet grepped.

### 6. `get_open_by_strategy_symbol` — `backend/app/db/repositories/position_repository.py:79`

```
$ grep -rn "get_open_by_strategy_symbol" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.8.db: binary file matches
grep: ./backend/app/db/repositories/__pycache__/position_repository.cpython-312.pyc: binary file matches
./backend/app/db/repositories/position_repository.py:79:    async def get_open_by_strategy_symbol(
```

A repository query with no caller. Worth confirming against the intended design: a
`(strategy, symbol)` position lookup is exactly what an exposure check would want, and
its absence is why #1/#2 rely on the in-memory `symbol_exposures` dict. **Question: was
this the intended DB-backed replacement for in-memory exposure tracking?** If so it is
unfinished work, not dead code.

### 7. `get_row` — `backend/app/db/repositories/trade_repository.py:27`

```
$ grep -rn "get_row" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.12.db: binary file matches
grep: ./backend/.mypy_cache/3.12/cache.1.db: binary file matches
./backend/app/db/repositories/trade_repository.py:27:    async def get_row(
grep: ./backend/app/db/repositories/__pycache__/trade_repository.cpython-312.pyc: binary file matches
```

One of the four forwarding methods on the wrapper in #32.

### 8. `is_trading_critical` — `backend/app/services/watchdog/state_machine.py:167`

```
$ grep -rn "is_trading_critical" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.11.db: binary file matches
grep: ./backend/app/services/watchdog/__pycache__/state_machine.cpython-312.pyc: binary file matches
./backend/app/services/watchdog/state_machine.py:167:def is_trading_critical(service_name: str) -> bool:
```

Definition only. Named as a trading-safety predicate, which makes its deadness worth a
second look before removal.

### 9. `list_all` — `backend/app/db/repositories/instrument_repository.py:96`

```
$ grep -rn "list_all" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
./backend/tests/test_service_control.py:56:def test_service_control_list_allowed(client: TestClient, mock_admin):
./backend/.pytest_cache/v/cache/nodeids:562:  "tests/test_service_control.py::test_service_control_list_allowed",
./backend/app/api/routes/service_control.py:103:async def list_allowed(
./backend/app/db/repositories/instrument_repository.py:96:    async def list_all(self) -> Sequence[InstrumentRecord]:
./docs/backend-persistence.md:76:| `InstrumentRepository` | `upsert`, `list_all` |
```

The only non-definition hits are substring matches on the unrelated `list_allowed`
route and its test. `docs/backend-persistence.md:76` documents it as part of the
repository's surface, which is stale.

### 10–11. `list_by_internal_order_id`, `list_by_internal_order_ids` — `backend/app/db/repositories/execution_repository.py:55`, `:63`

```
$ grep -rn "list_by_internal_order_id" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.13.db: binary file matches
grep: ./backend/app/db/repositories/__pycache__/execution_repository.cpython-312.pyc: binary file matches
./backend/app/db/repositories/execution_repository.py:55:    async def list_by_internal_order_id(self, internal_order_id: str) -> list[ExecutionModel]:
./backend/app/db/repositories/execution_repository.py:63:    async def list_by_internal_order_ids(
./docs/backend-persistence.md:74:| `ExecutionRepository` | `list_by_internal_order_id`, `upsert` |

$ grep -rn "list_by_internal_order_ids" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.13.db: binary file matches
grep: ./backend/app/db/repositories/__pycache__/execution_repository.cpython-312.pyc: binary file matches
./backend/app/db/repositories/execution_repository.py:63:    async def list_by_internal_order_ids(
```

**These two are the readers of the fill ledger, and nothing calls them.** Together with
deletion candidate 12 and Part A finding #26, this is the mechanism behind finding #19:
`ExecutionRepository` has one live method, `upsert`. The `executions` table is written
and never read.
`docs/backend-persistence.md:74` still lists `list_by_internal_order_id` as part of the
surface. **Recommend not deleting until #19 is resolved** — the correct fix there needs
exactly these queries.

### 12. `realized_pnl_from_marks` — `backend/app/db/repositories/execution_repository.py:39`

```
$ grep -rn "realized_pnl_from_marks" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.13.db: binary file matches
grep: ./backend/app/db/repositories/__pycache__/execution_repository.cpython-312.pyc: binary file matches
./backend/app/db/repositories/execution_repository.py:39:def realized_pnl_from_marks(
./docs/review/BUGS-lifecycle.md:190:  `realized_pnl_from_marks` (`execution_repository.py:39-46`) all use the same
```

Zero code references. Body is `return signed_qty * (exit_mark - entry)` (`:46`) —
identical arithmetic to `unrealized_leg` at `services/pnl.py:42`, `return signed_qty *
(mark - entry)`. A repo-wide search for this arithmetic finds only those two:

```
$ grep -rnE "\(\s*(exit|mark|close)[a-z_]*\s*-\s*(entry|open)[a-z_]*\s*\)|\(\s*(entry|open)[a-z_]*\s*-\s*(exit|mark|close)[a-z_]*\s*\)" --include=*.py --exclude-dir=.venv backend/app/
backend/app/services/pnl.py:41:    """Long: qty * (mark - entry). Short: negative qty * (mark - entry)."""
backend/app/services/pnl.py:42:    return signed_qty * (mark - entry)
backend/app/db/repositories/execution_repository.py:45:    """Long: qty * (exit - entry). Short uses negative qty the same way."""
backend/app/db/repositories/execution_repository.py:46:    return signed_qty * (exit_mark - entry)
```

Three names for one formula (`unrealized_leg`, `realized_pnl_from_marks`, and the
inline pair sum in `unrealized_pair` at `pnl.py:59-62`); the ledger-side one is dead.

### 13. `record_rejected_inbound` — `backend/app/services/order_manager.py:1342`

```
$ grep -rn "record_rejected_inbound" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.2.db: binary file matches
grep: ./backend/app/services/__pycache__/order_manager.cpython-312.pyc: binary file matches
./backend/app/services/order_manager.py:1342:    async def record_rejected_inbound(
./docs/archive/COMPLETE_TRADING_SYSTEM_DOCUMENTATION.md:648:| **Failure** | Parse `ValueError` → `_write_status REJECTED` + `record_rejected_inbound` with `reject_reason`; heartbeat checks `lease_lost` before terminal write. |
./docs/archive/COMPLETE_TRADING_SYSTEM_DOCUMENTATION.md:2956:| `async record_rejected_inbound(payload,capture_data,reason)` | Passes parse failure via `SignalRepository.record_rejected_payload` | — | — | `Recovery/worker` fallback |
```

**This is the most consequential entry in the list.** The archived documentation
describes it as the parse-failure persistence path invoked by the worker
(`"Parse ValueError → _write_status REJECTED + record_rejected_inbound"`), but no
caller exists. Either the documented behaviour regressed and unparseable payloads are
no longer recorded with a reject reason, or the doc describes an intent never wired up.
**Do not delete before confirming which** — if the former, this is a lost-signal path,
not bloat.

### 14. `set_cpu_percent_fn` — `backend/app/services/watchdog/resources.py:293`

```
$ grep -rn "set_cpu_percent_fn" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.10.db: binary file matches
grep: ./backend/app/services/watchdog/__pycache__/resources.cpython-312.pyc: binary file matches
./backend/app/services/watchdog/resources.py:293:    def set_cpu_percent_fn(self, fn: Callable[[], float] | None):
```

A test seam with no test using it.

### 15. `telegram_configured` — `backend/app/services/watchdog/config.py:80`

```
$ grep -rn "telegram_configured" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.15.db: binary file matches
grep: ./backend/app/services/watchdog/__pycache__/config.cpython-312.pyc: binary file matches
./backend/app/services/watchdog/config.py:80:    def telegram_configured(self) -> bool:
```

Definition only. Note the watchdog *does* send Telegram alerts
(`services/watchdog/telegram.py`, wired at `daemon.py:94`) — it just never asks whether
Telegram is configured first.

### 16. `critical_reserved` — `backend/app/services/watchdog/notifier.py:714`

```
$ grep -rn "critical_reserved" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.13.db: binary file matches
grep: ./backend/app/services/watchdog/__pycache__/notifier.cpython-312.pyc: binary file matches
./backend/app/services/watchdog/notifier.py:714:    def critical_reserved(self) -> int:
```

### 17. `_severity` — `backend/app/services/watchdog/notifier.py:41`

```
$ grep -rn "_severity" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
./backend/tests/test_watchdog_telegram_detail.py:44:def test_severity_critical():
./backend/tests/test_watchdog_telegram_detail.py:51:def test_severity_warning():
./backend/tests/test_watchdog_telegram_detail.py:58:def test_severity_info():
./backend/.pytest_cache/v/cache/nodeids:788:  "tests/test_watchdog_telegram_detail.py::test_severity_critical",
./backend/app/services/watchdog/notifier.py:41:def _severity(event: NotificationEvent) -> tuple[str, str]:
```

The three test hits are substring matches on test *names* (`test_severity_critical`),
not calls to `_severity`. Tests named after a function that they do not invoke —
patch residue from a refactor that inlined it.

### 18. `_tcp_open` — `backend/app/services/watchdog/health.py:44`

```
$ grep -rn "_tcp_open" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
./backend/tests/test_watchdog_pre_step9_fixes.py:42:    monkeypatch.setattr("app.services.watchdog.health._tcp_open_async", fake_tcp)
./backend/app/services/watchdog/health.py:44:def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
./backend/app/services/watchdog/health.py:45:    """Sync TCP open used only in fallback paths; prefer _tcp_open_async in async checks."""
./backend/app/services/watchdog/health.py:53:async def _tcp_open_async(host: str, port: int, timeout: float = 1.0) -> bool:
./backend/app/services/watchdog/health.py:155:        tcp_ok = await _tcp_open_async(host, port, timeout=1.0)
./backend/app/services/watchdog/health.py:361:        if await _tcp_open_async(host, port):
./backend/app/services/watchdog/health.py:449:        if await _tcp_open_async(host, port):
./backend/app/services/watchdog/health.py:557:            if await _tcp_open_async(host, port):
./backend/app/services/watchdog/health.py:599:        if not await _tcp_open_async(host, port, timeout=1.0):
./backend/app/services/watchdog/health.py:679:        tcp_ok = await _tcp_open_async(host, port, timeout=1.0)
```

Every reference is to `_tcp_open_async`. The sync `_tcp_open` has no caller, and its own
docstring says it is "used only in fallback paths" — there are no such paths left. Its
`timeout: float = 1.0` default is a fourth copy of the 1.0 s health timeout that the
six async call sites pass explicitly.

### 19. `_service_display` — `backend/app/services/watchdog/health.py:133`

```
$ grep -rn "_service_display" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git .
grep: ./backend/.mypy_cache/3.12/cache.8.db: binary file matches
grep: ./backend/app/services/watchdog/__pycache__/health.cpython-312.pyc: binary file matches
./backend/app/services/watchdog/health.py:133:def _service_display(service: ServiceName) -> str:
```

### 20. `is_processed` — `backend/app/db/repositories/signal_repository.py:147`

```
$ grep -rn "is_processed" --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git \
    --exclude-dir=__pycache__ --exclude-dir=.mypy_cache --exclude-dir=.pytest_cache .
./backend/app/db/repositories/signal_repository.py:147:    async def is_processed(self, strategy_id: str, signal_id: str) -> bool:
./docs/backend-persistence.md:67:| `SignalRepository` | `get_by_strategy_signal`, `is_processed`, `list_processed_open_keys`, `record_inbound`, `record_processed`, `record_rejected_payload` |
```

Definition plus one stale doc row. The live duplicate barrier is
`list_processed_open_keys` (`:151`), which feeds `RMSContext.processed_signals`. This is
a superseded per-signal check left in place beside its bulk replacement — and its
signature, `(self, strategy_id: str, signal_id: str)`, takes the same un-normalized
pair as finding #5.

### 21. `Order` alias — `backend/app/oms/models.py:172`

```
$ grep -rn "Order = OMSOrder" --include=*.py --exclude-dir=.venv backend/
backend/app/oms/models.py:172:Order = OMSOrder

$ grep -rnE "(from app\.oms\.models import .*\bOrder\b[^A-Za-z])|(^\s+Order,$)" --include=*.py --exclude-dir=.venv backend/
(no output)
```

See #33. The sibling alias `OrderStatus` (`oms/models.py:25`) is imported only by
`tests/test_order_manager.py:9`; production re-derives it as
`from app.oms.models import OMSOrderStatus as OrderStatus` (`schemas/api_schemas.py:8`).

### Also unused, lower confidence

`IBKR_DOCUMENTED_CEILING_MSG_PER_SEC` (#36) and the `ExecutionTimestamps` latency
properties. The latter are used only by a standalone script, not the application:

```
$ grep -rn "rms_latency_ms" --include=*.py --exclude-dir=.venv backend/app backend/scripts scripts/
backend/app/oms/models.py:42:    def rms_latency_ms(self) -> float | None:
backend/scripts/oms/run_paper_execution.py:223:        print(f"  RMS Latency      : {format_duration(final_order.timestamps.rms_latency_ms)}")
```

`oms_latency_ms`, `ibkr_submit_latency_ms`, `submit_to_fill_ms`, and
`total_intent_to_fill_ms` (`oms/models.py:48-75`) follow the same pattern. These are
latency instrumentation reachable only from a developer harness — keep or promote to a
metrics path, but they are not part of the running system today. **Not recommending
deletion**; flagging that the execution-latency instrumentation is currently
unobservable in production.

---

## Normalization gaps

Every site found that compares, hashes, or keys on an identifier. **N** = value is not
normalized at that site. These feed the concurrency review: rows 1–8 are `asyncio.Lock`
keys or dedup barriers.

| Site | Identifier | Normalized? | Notes |
|---|---|---|---|
| `services/order_manager.py:988` `exposure_key(intent, leg.symbol)` → `_exposure_locks` key | symbol | **N** | `asyncio.Lock` key. Finding #1. Account key on `:991` normalized in the same statement. |
| `services/order_manager.py:991` `("__margin__", intent.ibkr_account.strip().upper())` | account | Y | `.strip().upper()`. |
| `services/order_manager.py:994` `("__model_value__", *value_key)` via `model_value_key` | strategy_id | **N** | `rms/models.py:127` `return (intent.account_id, intent.strategy_id)`. Lock key. |
| `services/worker_pool.py:85` `key = (account_scope or "default", strategy_id)` | strategy_id | Y (upstream) | Lock key. `strategy_id` arrives from `compute_idempotency_key`, which lowercases via `normalize_strategy_id` (`worker_pool.py:35`). The only lock key in the system that is normalized. `account_scope` is separately never populated. |
| `rms/checks/money_per_stock.py:56` `per_symbol_limits.get((intent.account_id, symbol))` | symbol | **N** | Money-cap lookup. Finding #2. Limits stored UPPER at `accounts/config_service.py:407,430`. |
| `rms/checks/money_per_stock.py:68` `symbol_exposures.get(exposure_key(intent, symbol), ...)` | symbol | **N** | Money-cap read. Finding #2. |
| `rms/checks/duplicate.py:27-29` `lookup_key = (intent.strategy_id, intent.signal_id)` | strategy_id, signal_id | **N** | Duplicate-order barrier. Finding #5. |
| `services/order_manager.py:1283` `processed_signals.add(duplicate_lookup_key(intent))` | strategy_id, signal_id | **N** | Writer for the barrier above. |
| `services/order_manager.py:611,618` `key_a = (row.account_id, row.leg_a_symbol)` | symbol | **N** | Exposure hydration. Finding #4. |
| `services/order_manager.py:1243,1289,1306` `exp_key = exposure_key(intent, leg.symbol)` | symbol | **N** | Exposure read-modify-write, three sites. |
| `services/order_manager.py:249` `per_symbol_limits[(limit.account_id, limit.symbol)]` | symbol | **N** | Raw from DB row; relies on the writer having normalized. |
| `rms/checks/position_limit.py:44` `open_positions.get(open_position_key(intent), 0)` | strategy_id | **N** | Position-cap read via the helper. |
| `rms/checks/position_limit.py:47-49` `account_open_limits.get((intent.account_id, intent.strategy_id))` | strategy_id | **N** | Position-cap override. Key is hand-built inline and is byte-identical to what `model_value_key` (`rms/models.py:127`) returns — a fifth independent construction of the same `(account_id, strategy_id)` key. |
| `rms/checks/model_market_value.py:101` `model_value_used.get(key, Decimal(0))` | strategy_id | **N** | Market-value cap; `model_value_key` (`rms/models.py:123`). |
| `services/position_reconciler.py:141,360` `ibkr_to_account.get(line.ibkr_account)` | account | **N** | Finding #6. Symbol and sec_type normalized on the adjacent lines. |
| `services/position_reconciler.py:354` `{acc.ibkr_account: acc.id for acc in accounts}` | account | **N** | Map construction for the above. |
| `services/critical_recovery.py:348,353` same pair | account | **N** | Finding #6. |
| `services/reconcile_service.py:67,72` same pair, plus raw API query param | account | **N** | Findings #6, #7. |
| `api/routes/reconcile.py:43,68` `o.account == user_account` style comparison | account | **N** | Authorization. Finding #7. |
| `api/routes/orders.py:30,50,72` `o.account == user_account` | account | **N** | Authorization, and the attribute does not exist. Finding #8. |
| `services/model_blue/parser.py:140` `symbol = str(...).strip()` | symbol | **N** | Root cause. Finding #3. `.strip()` without `.upper()`. |
| `services/model_blue/persistence.py:37` `marks[order.symbol] = raw` | symbol | **N** | Exit-mark dict keyed on raw order symbol; consumed by `close_trade` for realized P&L. |
| `services/pnl.py:159` `legs[leg.symbol] = (signed, leg.price)` | symbol | **N** | Mark dict key. `_request_ticks` computes `sym_clean = (leg.symbol or "").strip().upper()` at `:512` but uses it only for the synthetic-symbol test, then keys `_by_req`/`_listeners_by_req` on raw `leg.symbol` (`:627-628, :640, :643`) while keying `_contract_reqs` on the *resolved contract* symbol (`:610`). Two symbol vocabularies in one method. |
| `services/pnl.py:151` `intent.signal_id.split(":CLOSE")[0].split(":UNWIND:")[0]` | trade_id | **N** | Derived by suffix-stripping rather than `normalize_trade_id`. Finding #28. |
| `rms/margin_estimate.py:48` `key = (symbol.strip().upper(), itype, side_key)` | symbol | **Y** | The counter-example: margin rates normalize. |
| `services/margin_rate.py:37-39,59-61,101` | symbol, type, side | **Y** | Normalized on load and on write. |
| `instruments/resolver.py:101-107` `wanted_sym = symbol.strip().upper()` | symbol | **Y** | Instrument resolution normalizes both sides. |
| `db/repositories/instrument_repository.py:108-114` | symbol, sec_type | **Y** | |
| `services/position_reconciler.py:94,108,145,159` `_norm_symbol` / `_norm_sec_type` | symbol, sec_type | **Y** | Both sides of the broker-vs-ledger comparison. |
| `services/critical_recovery.py:354-355` | symbol, sec_type | **Y** | |
| `services/broker_flatten_service.py:99-114` | symbol, sec_type | **Y** | Normalizes both sides before flattening. |
| `rms/checks/margin.py:57` `account = (intent.ibkr_account or "").strip().upper()` | account | **Y** | |
| `services/account_margin.py:227,238` | account | **Y** | |
| `oms/ibkr_adapter.py:133,141,159` | account | **Y** | |
| `services/order_manager.py:378,392,413,477` | account | **Y** | |
| `api/routes/config.py:64,144`, `api/routes/baskets.py:30`, `api/routes/margin.py:124,127` | account | **Y** | |
| `db/repositories/basket_repository.py:58`, `accounts/config_service.py:162,274` | account | **Y** | |
| `services/model_blue/parser.py:21` `(strategy_id or "").strip().lower()` | strategy_id | **Y** | |
| `core/identifiers.py:17,27` | strategy_id, trade_id | **Y** | One importer. Finding #9. |

**Summary of the pattern.** Symbol and account are normalized consistently in the
instrument-resolution, margin, and reconciliation subsystems, and consistently *not*
normalized in the RMS-context subsystem — the exposure locks, the money cap, the
position cap, the market-value cap, and the duplicate barrier. The two subsystems meet
at `order_manager.py:987-994`, where the account component of a lock key is normalized
and the symbol component of the same key is not.
