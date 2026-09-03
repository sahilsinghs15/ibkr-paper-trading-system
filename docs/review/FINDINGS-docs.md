# Documentation audit — docs vs code

**Method.** `docs/review/MAP.md` (code-derived) used as ground truth, with every claim
re-verified directly against the source before being recorded here. Code is authoritative;
where the code looks like the defect rather than the prose, it is called out in §3.

**Read at:** working tree of `/home/tradingapp/app`, HEAD `259739b`
(2026-09-02 19:17, *"fix(watchdog): set WatchdogSettings gateway_port default to 4001"*).
Doc paths are relative to `/home/tradingapp/app`; code paths are relative to the same root.

**Diagnosis only.** Nothing was rewritten in this pass.

---

## 0. Inventory

### 0.1 The drift window

Most of the curated doc tree was last touched on **2026-08-28** by two commits
(`acdd451`, `f41541d`). Five architectural patches landed *after* that and are the source of
most findings below:

| Commit | Date | What changed |
|---|---|---|
| `28f5325` | 08-31 21:16 | **user authentication, RBAC, cross-account isolation** — added `users`, `auth.py`, `deps.py` JWT, `jwt_*` settings |
| `ca90d1f` | 08-31 22:01 | REST auth restricted to JWT; authorization tests |
| `394e616` / `82252f1` | 09-01 17:35 / 17:52 | **systemd-native service management**; `service_control.py` route; `process_manager` deprecated |
| `abd604e` | 09-02 17:01 | **RMS checks 7 and 8 made fail-closed**; `$10M` default removed from `accounts.default_symbol_limit` |
| `259739b` | 09-02 19:17 | `WatchdogSettings.gateway_port` default `4002` → **`4001`** |

Anything dated `2026-08-28` or earlier is therefore pre-auth, pre-systemd and
pre-fail-closed-RMS. That is the highest-yield hunting ground and it is where the P0/P1
findings cluster.

### 0.2 Artifacts

| Path | Lines | Last commit | Purpose | Pre-patch? |
|---|---|---|---|---|
| `AGENTS.md` | 102 | 08-31 `8cbe353` | Agent entry point: invariants, run commands, doc router | pre-auth, pre-systemd |
| `README.md` | 177 | 08-31 `8cbe353` | Human documentation portal, 13 sections | pre-auth, pre-systemd |
| `docs/README.md` | 108 | 08-31 `8cbe353` | Doc index **plus per-document accuracy verdicts** | pre-auth, pre-systemd |
| `docs/backend-execution.md` | 173 | 08-31 `8cbe353` | Signal path + log greps (debug runbook) | pre-auth |
| `docs/backend-map.md` | 188 | 08-31 `8cbe353` | Package tree, lifespan, `app.state`, constants | pre-auth |
| `docs/backend-rms-oms.md` | 189 | 08-31 `8cbe353` | RMS check table, basket states, adapter, limiter | pre-fail-closed |
| `docs/backend-config.md` | 86 | 08-31 `8cbe353` | **The** env var reference | pre-auth |
| `docs/watchdog.md` | 127 | 08-31 `8cbe353` | Watchdog health/gates/systemd | pre-systemd, pre-4001 |
| `docs/backend-concurrency.md` | 236 | 08-28 `acdd451` | Jobs, leases, claims, recovery | **yes** |
| `docs/backend-api.md` | 106 | 08-28 `acdd451` | HTTP endpoint inventory | **yes** |
| `docs/backend-testing.md` | 111 | 08-28 `acdd451` | Test inventory + commands | **yes** |
| `docs/conventions.md` | 73 | 08-28 `acdd451` | Route/DI conventions | **yes** |
| `docs/frontend.md` | 124 | 08-28 `acdd451` | Vite dashboard | **yes** |
| `docs/gaps.md` | 68 | 08-28 `acdd451` | Explicit not-implemented list | **yes** |
| `docs/safety.md` | 90 | 08-28 `acdd451` | Paper vs live, pacing, disk retention | **yes** |
| `docs/EC2_OPERATIONS_GUIDE.md` | 409 | 08-28 `acdd451` | EC2 ops runbook (self-dated 18 Aug) | **yes** |
| `docs/backend-kill-switch.md` | 186 | 08-28 `f41541d` | Kill-switch semantics + sidecar runbook | **yes** |
| `docs/backend-multi-gateway.md` | 401 | 08-28 `f41541d` | **Rate-limiting / multi-gateway design notes** | **yes** |
| `docs/backend-persistence.md` | 104 | 08-28 `f41541d` | **Schema doc**: tables, migrations, repos | **yes** |
| `docs/OPERATOR_TELEGRAM_ALERTS.md` | 388 | 09-01 `01739e2` | Non-technical operator alert runbook | pre-4001 only |
| `backend/AGENTS.md` | 15 | 08-24 `4fb14dc` | Pointer stub | **yes** |
| `backend/README.md` | 53 | 08-28 `acdd451` | Backend runbook | **yes** |
| `frontend/README.md` | 56 | 08-24 `4fb14dc` | Frontend runbook | **yes** |
| `backend/.env.example` | 61 | — | Env template operators copy | **yes** |
| `backend/POSTMAN_API_TESTING_GUIDE.md` | 847 | 08-10 `d2fc823` | Historical API guide, **no stale banner in the file itself** | **yes** |
| `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` | 258 | 08-19 `af1a267` | Historical dev guide, has a stale banner | **yes** |
| `docs/archive/*.md` (10 files) | 6374 | 08-31 `8cbe353` | Frozen snapshots incl. `production_mft_ibkr_pacing.md` (a second, conflicting pacing design note) | **yes** |
| `/home/tradingapp/pair-allocation-and-model-market-value-spec.md` | 1422 | untracked, 09-02 | Phased build instructions for pair allocation + check 101 | partly built |

### 0.3 Architectural claims in code, not under `docs/`

These carry load-bearing prose and were audited as documentation:

| Location | Claim |
|---|---|
| `backend/app/db/models/execution_claim.py:1-8` | `execution_claims` is *"the authoritative barrier"* vs in-memory sets — **accurate** |
| `backend/app/services/order_manager.py:974-986` | Why the exposure lock exists and why sorted acquisition prevents deadlock — **accurate, invariant holds** (`:995-1001`) |
| `backend/app/services/model_blue/sizer.py:1-17` | Weight-proportional sizing rules — **accurate** (was stale per the root spec; has since been fixed) |
| `backend/app/api/routes/emergency.py:29-32` | *"Fails closed (401) if secret is unconfigured when enabled"* — **accurate** (`:40-47`) |
| `backend/app/services/margin_scanner.py:5` | Private bucket needed *"because GatewayRateLimiter priority does not isolate"* — **accurate** (`gateway_rate_limiter.py:259-274`) |
| `backend/app/oms/retry_policy.py:11` | *"Retries are demo/paper Gateway/TWS ports only (not 7496/4001 live)"* — **accurate** |
| `backend/app/api/routes/webhooks.py:30` | *"TEMPORARY: append-only CSV of every accepted webhook. Remove later."* — accurate, and it is the **only** thing written to disk (see #1) |
| `scripts/process_manager.py:1-45` | Full supervisor design docstring with **no deprecation notice**, despite `82252f1` deprecating it |

### 0.4 Dangling reference

`Execution_System_Architecture.md` — the "target architecture" document that four artifacts
tell the reader to consult — **does not exist** anywhere on the host or in git history
(`git log --all -- 'Execution_System_Architecture.md'` returns nothing;
`find / -iname '*rchitect*'` finds no such file). See #24.

---

## 1. Findings

| # | Severity | Doc location | Code location | Documented claim | Actual behaviour | Which is wrong | Confidence |
|---|---|---|---|---|---|---|---|
| 1 | **P0** | `backend/.env.example:9-12` | `backend/app/core/config.py:20-25` | `# Broker mode: "mock" (default, no TWS) or "ibkr" (requires TWS/Gateway)` / `BROKER_MODE=mock` / `# IBKR connection (only used when BROKER_MODE=ibkr)` | `Settings` has no `broker_mode` field and `extra="ignore"`, so the key is silently dropped. `main.py:105` connects to `ibkr_host:ibkr_port` and `ibkr_adapter.py:438` calls `placeOrder` unconditionally. There is no MockBroker class in the repo. | **Doc.** An operator who copies `.env.example` to `.env` believes orders are mocked while they are being sent to a live socket. Every other doc flags `BROKER_MODE` as ignored; the template that operators actually copy does not. | certain |
| 2 | **P0** | `docs/backend-config.md:53-54` | `backend/app/api/routes/webhooks.py:147-160` | `webhook_auth_secret` … ``None` = auth disabled when `webhook_auth_enabled=false`` — implying auth is enforced while `webhook_auth_enabled=True` (the default) | `_verify_webhook_authentication` returns early if the toggle is off, then `if expected_secret:` at `:152` — when the secret is `None` (the default, `config.py:110`) **the header is never checked and no warning is logged**. `WEBHOOK_AUTH_ENABLED=true` + unset secret = fully open endpoint. | **Both.** Doc understates the failure mode; the code fails *open* where its sibling `emergency.py:40-47` fails *closed* with the same shape. See §3.1. | certain |
| 3 | **P0** | `backend/.env.example` (whole file) | `backend/app/core/config.py:33`, `:38`, `:110`, `:114` | The template lists 30 keys and never mentions `DATABASE_URL`, `JWT_SECRET_KEY`, `WEBHOOK_AUTH_SECRET`, or `EMERGENCY_KILLSWITCH_AUTH_SECRET` | All four exist as `Settings` fields. `jwt_secret_key` defaults to the literal `"PRODUCTION_JWT_SECRET_KEY_CHANGE_IN_ENV_MUST_BE_SECURE_32_BYTES"`. | **Doc.** Following the template ships a live-money API with a publicly-known JWT signing key and an unauthenticated webhook. | certain |
| 4 | **P0** | `docs/backend-config.md:11-57` | `backend/app/core/config.py:38-42` | The `Settings` field table is presented as complete ("Every `Settings` / demo env field" per `docs/README.md:24`) and contains **no** JWT row | `jwt_secret_key` (`:38`), `jwt_algorithm` (`:41`), `jwt_access_token_expire_minutes` (`:42`, default `480`) are all `Settings` fields, added by `28f5325` after this doc's last edit. | **Doc.** The one documented env reference omits the secret that gates admin access to `/square-off`, `/kill-switch/clear` and `/service-control`. | certain |
| 5 | **P0** | `docs/EC2_OPERATIONS_GUIDE.md:346` (checklist), `:116`, `:144`, `:311-318`, `:376` | filesystem | Pre-order health checklist item: `- [ ] YAML allocation present`; secrets table entry `Paper capital YAML | /home/tradingapp/app/backend/config/paper_allocations.yaml`; `There is **no** DATABASE_URL on EC2 today. Capital comes from YAML.` | `backend/config/` **does not exist**; `find . -name '*allocations*.yaml'` returns nothing. Capital is `accounts.total_margin × allocations.alloc_pct × allocations.pair_max_allocation_pct` resolved by `backend/app/accounts/router.py:63`. `ALLOCATIONS_CONFIG_PATH` is dropped by `extra="ignore"`. | **Doc.** The pre-trade checklist an operator runs before releasing a live order contains an unsatisfiable item and points capital configuration at a file that does not exist, away from the DB rows that actually size orders. | certain |
| 6 | **P0** | `docs/EC2_OPERATIONS_GUIDE.md:206-258` (§6 start procedure), `:173-192` (§5 tmux) | `deploy/systemd/*` (14 units), `scripts/ibgateway-wrapper.sh` | "**Source of truth:** `/home/tradingapp/start.txt`. Do not invent a different Gateway launcher" — then manual `uv run uvicorn`, manual `Xvfb :99`, manual `~/ibc/scripts/ibcstart.sh 1045`, all inside tmux panes | Since `394e616`/`82252f1` the stack is systemd-native: `trading-backend.service` (`Restart=always`), `webhook-ingest.service`, `ibgateway.service` (`ExecStart=…/scripts/ibgateway-wrapper.sh`, lifecycle owned by `trading-session-start.timer` / `trading-session-stop.timer`), plus `*-restart.path` units. `trading-backend.service` restarts on its own. | **Doc.** Following §6 while systemd is active starts a **second** uvicorn on `:8001` and a **second** IBC/Gateway login — precisely the "Second Gateway login while one is healthy" that the same file's never-do list forbids at `:364`. | certain |
| 7 | **P1** | `docs/backend-multi-gateway.md:29` | `backend/app/broker/ibkr/tws_client.py:89-101`; `backend/app/oms/ibkr_adapter.py:144-166`, called `:393` | "There is no `reqManagedAccts` / `managedAccounts` handling in `TWSClient`." Reinforced at `:330`: "`ib_order.account` assumes one authorized login \| Independent logins silently fail or trade the wrong account \| S (docs/validation)" | `TWSClient.managedAccounts` (`:89`) populates `self.managed_accounts`; `_validate_ibkr_account` (`ibkr_adapter.py:144`) is a fail-closed gate that sets `OMSOrderStatus.ERROR` and returns `False` for `MISSING_IBKR_ACCOUNT` (`:149`), `UNMANAGED_ACCOUNT: managedAccounts not yet received from gateway` (`:156`) and `UNMANAGED_ACCOUNT: '{account}' is not in gateway managedAccounts` (`:163`) — **before** `placeOrder`. Covered by `tests/test_ibkr_adapter_managed_accounts.py`. | **Doc.** A load-bearing safety gate is documented as absent, in the same file that a reader consults when changing account routing. `docs/backend-map.md:159` and `docs/backend-rms-oms.md:139` describe it correctly, so the doc set contradicts itself. | certain |
| 8 | **P1** | `docs/backend-multi-gateway.md:336` | `backend/app/oms/ibkr_adapter.py:949-950`; `backend/app/broker/ibkr/tws_client.py:129-130`; `gateway_rate_limiter.py:102-115` | Gap-analysis row: "No Error 100 handling \| IB throttle is invisible except logs \| S \| Limiter cooldown" | `notify_error_100()` drains both buckets to `0.0` and sets `_cooldown_until = now + error100_cooldown_sec`, wired from **two** call sites. The same doc contradicts this at `:56` and `:82`. | **Doc.** Listing an implemented throttle-backoff as missing invites someone to "add" it or to conclude the system is unprotected during an Error 100 incident. | certain |
| 9 | **P1** | `docs/backend-multi-gateway.md:341` | `backend/app/oms/ibkr_adapter.py:244-248`; `gateway_rate_limiter.py:259-274` | Gap row: "Kill-switch flatten uses same 0.2s pacer, no priority \| Emergency close delayed by ordinary submits" | `_order_priority` returns `PRIORITY_EMERGENCY_FLATTEN` (0) for `ExecutionIntentMode.EMERGENCY_FLATTEN`, and `_try_consume_locked` lets P0 spend a global token **without** requiring a normal token (`:266-269`). There is no "0.2s pacer" in the trading process at all — `--pace 0.2` belongs to the offline sidecar `scripts/oms/flatten_gateway_positions.py:229`. | **Doc.** Says emergency flatten has no priority when it does, referencing a pacer that was removed (`:61` of the same file says `OrderSubmitPacer` was removed). | certain |
| 10 | **P1** | `docs/backend-multi-gateway.md:44` | `backend/app/services/pnl.py:121-124` | "Market-data `reqMktData` shares the one socket (and therefore the one pacer is **not** applied to those messages — pacer is `placeOrder` only)." | `self._rate_limiter.try_acquire(PRIORITY_MARKET_DATA, request_type)` gates every subscription. The same doc says the opposite at `:56` ("`reqMktData` P3 try_acquire **Done**") and `:354`. | **Doc**, and it disagrees with itself in three places. | certain |
| 11 | **P1** | `docs/backend-rms-oms.md:55-63` (OPEN vs CLOSE table), `:16-17` | `backend/app/rms/checks/money_per_stock.py:60-66`; `backend/app/rms/checks/position_limit.py:53-59` | Check 8 row: `| 8 Money per stock | PASS | PASS | Per-symbol notional cap |`; check 7: `| 7 Position limit | PASS | — | Cap from allocation |`. No mention of rejection on missing configuration. | `abd604e` replaced check 8's `continue` with `REJECT … NO_SYMBOL_LIMIT_CONFIGURED: No per-symbol limit configured for account {id} and symbol '{symbol}'`, and added check 7's `REJECT … INVALID_MAX_POSITIONS_LIMIT` when `max_positions is None or <= 0`. | **Doc.** Two RMS checks changed from skip-if-unconfigured to reject-if-unconfigured and no doc records it. An operator adding a symbol or an allocation with `max_open_positions = 0` will see every OPEN rejected with a reason string that appears in no document. | certain |
| 12 | **P1** | `docs/backend-api.md:32` ("Endpoints (complete list)"), `:27` | `backend/app/api/router.py:17-25`; `backend/app/api/routes/{auth,service_control,health,config}.py` | Header says **complete list**; `:27` says `api_router` mounts "orders + baskets + config + margin + system-monitor + reconcile routers". Not one row mentions authentication. | `api_router` includes **nine** routers — the three omitted are `auth_router`, `emergency_router` and `service_control_router`. Missing endpoints: `GET /health/live`, `GET /health/ready` (`health.py:14`, `:20`), `POST /api/v1/auth/login` (`auth.py:49`), `POST /api/v1/auth/sse-token` (`:99`), `GET /api/v1/auth/me` (`:112`), `POST /api/v1/config/accounts/{id}/positions/{trade_id}/close` (`config.py:295`), `PUT /api/v1/config/accounts/{id}/default-symbol-limit` (`config.py:627`), `POST /api/v1/service-control/{service}/{action}` (`service_control.py:34`), `GET /api/v1/service-control/allowed` (`:99`). MAP counts 37 routes; the doc lists 24. Every listed route is missing its auth requirement (JWT bearer via `deps.py:56`, admin gate via `deps.py:112`). | **Doc.** The `service-control` omission is the sharp one: an admin HTTP route that shells out to `systemctl` (`service_control.py:7`, allowlist `:20-26`) appears in no API document. | certain |
| 13 | **P1** | `docs/backend-api.md:89` | `backend/demo_streaming/api.py:303-331` | Demo table row: `GET/POST/PATCH/PUT/DELETE | /api/v1/config/* | Proxy to trading app` | The route is `@app.api_route("/api/v1/{full_path:path}", methods=[...])` — a catch-all over **all** of `/api/v1`, not just `config`. It carries no `Depends` (no `get_current_user`, no `require_admin`) and forwards headers verbatim (`:315`). Every mutating trading route, including `/config/accounts/{id}/square-off` and `/service-control/{service}/{action}`, is reachable on `:8010` at the same path. | **Doc.** Narrowing the proxy to `config/*` materially understates the surface exposed when `DEMO_STREAM_HOST=0.0.0.0` — which `backend/README.md:47` and `AGENTS.md:79` both suggest. | certain |
| 14 | **P1** | `docs/watchdog.md:23`, `:27` | `backend/app/services/watchdog/config.py:41-44`; `health.py:31-36` | `| Gateway `:4002` | TCP 127.0.0.1:4002 open | …` and `| PostgreSQL | `SELECT 1` else TCP 5432 |` | `gateway_port` now defaults to **`4001`** (`config.py:42`, changed by HEAD commit `259739b`) with aliases `GATEWAY_PORT`/`WATCHDOG_GATEWAY_PORT`/`IBKR_PORT`. Postgres host/port are derived from `database_url` via `make_url` (`health.py:31-33`), falling back to `postgres_port` — the default URL is port **5433**, not 5432. | **Doc.** During a "gateway down" alert an operator reading this doc probes 4002 while the watchdog probes 4001. `docs/OPERATOR_TELEGRAM_ALERTS.md:134-135` repeats `:4002`. | certain |
| 15 | **P1** | `docs/watchdog.md:9-16`, `:100-108` | `deploy/systemd/` (14 files); `scripts/process_manager.py` | Ownership table: "process_manager.py \| `process_manager` \| Gateway (Xvfb+IBC), Backend `:8001`, Webhook `:8000`" and "No competing supervisors … process_manager owns restarts". Systemd block names only `process-manager.service`, `demo-streaming.service`, `watchdog.service`. Install: `sudo systemctl enable --now process-manager watchdog demo-streaming`. | `82252f1` deprecated `process_manager` for systemd-native management. `deploy/systemd/` now contains first-class `trading-backend.service` (`Restart=always`), `webhook-ingest.service`, `ibgateway.service`, `trading-session-{start,stop}.{service,timer}`, `trading-backend-restart.{path,service}`, `demo-streaming-restart.{path,service}`. Three independent restart owners now exist (systemd units, the `.path` triggers, `process_manager`) — MAP §8.7 records this as unresolved. | **Doc.** The documented install command leaves the trading backend and webhook ingest unmanaged, and the ownership table names a supervisor that has been deprecated. | certain |
| 16 | **P1** | `docs/README.md:47-71` | this file | The "Doc inventory (accuracy vs code)" table labels `backend-execution.md`, `backend-map.md`, `backend-concurrency.md`, `backend-kill-switch.md`, `backend-api.md`, `backend-config.md`, `backend-persistence.md`, `backend-rms-oms.md`, `safety.md`, `gaps.md`, `../AGENTS.md`, `../README.md` all **ACCURATE** | Findings #4, #7, #11, #12, #17, #18, #19, #20, #21, #22, #25 contradict that verdict. The table itself is dated by the `2026-08-28` changelog below it. | **Doc.** This is the artifact a reader consults *to decide what to trust*. A stale accuracy table is worse than no accuracy table: it converts a reader's healthy suspicion into misplaced confidence. | certain |
| 17 | **P1** | `docs/safety.md:54`; `docs/gaps.md:41` | `backend/demo_streaming/api.py:43-79`, `:148`, `:177`, `:204`, `:247` | "The PnL + Settings UI on `:8010` has **no auth**." / "No in-app auth on the `:8010` dashboard" | `_get_authenticated_user_from_request` (`:43`) now gates `/demo/positions`, `/demo/closed-positions`, `/demo/signals` and `/demo/stream`. Query tokens must be short-lived SSE tokens (`decode_sse_token`, `:57`); header tokens must be access JWTs (`:64`). Auth exists — but **not** on the `/api/v1/*` proxy (#13), and `TRADINGAPP_TESTING=1` returns a synthetic admin (`:69-77`). | **Doc.** Wrong in both directions at once: it denies the auth that exists on `/demo/*` and stays silent about the proxy that genuinely has none. Neither half is safe to act on. | certain |
| 18 | **P1** | `docs/backend-execution.md:40-43` | `backend/app/services/order_manager.py:743-757` | Live-path diagram orders the per-account steps as: `asyncio.gather per AccountExecutionContext` → `kill-switch gate on OPEN` → `ModelBlueStrategy.build_intent` | Real order in `_fanout_single_account`: `_assert_account_has_free_margin(ctx)` on OPEN (`:745`) → `handler.build_intent(...)` (`:746`) → kill-switch gate (`:748`). The kill-switch gate is *after* intent construction, and the **margin free-funds gate is absent from the diagram entirely**. | **Doc.** Two errors in one node list: wrong order, and an undrawn pre-RMS money gate. `docs/backend-rms-oms.md:42` describes Gate A correctly, so the two docs disagree. | certain |
| 19 | **P1** | `docs/backend-execution.md:44-49` | `backend/app/services/order_manager.py:1050`, `:1057-1071` | The live path goes `RMSEngine.evaluate` → `instrument resolve` → `execution_claims.acquire` → `BasketCoordinator.execute` | Two steps between resolve and claim are undrawn: `_confirm_margin_if_borderline` (`:1050` → `:468`), which issues a real `placeOrder(whatIf=True)` to IBKR (`ibkr_adapter.py:367`); and the **basket-critical gate** (`:1057-1071` → `coordinator.py:88`) that blocks new OPENs for a `(account, strategy)` with an unresolved CRITICAL basket. | **Doc.** Both are safety mechanisms and both are load-bearing. `docs/backend-concurrency.md:147` names the CRITICAL gate correctly ("after RMS PASS + CRITICAL gate + instrument resolve"), so the primary debug runbook is the one that is wrong. | certain |
| 20 | **P1** | `docs/backend-persistence.md:8-27` | `backend/app/db/models/user.py:28`; `alembic/versions/g1h2i3j4k5l6_create_users_table.py:21` | The "Postgres tables (ORM)" table lists 19 tables and is the schema reference for the repo | There are **20**. `users` is missing from the table list, even though this same doc lists its migration `g1h2i3j4k5l6` at `:55`. `users` holds `role` (`admin`/non-admin) and gates every authenticated route. | **Doc.** The schema doc omits the table that the entire authorization model depends on, and its own migration list proves the omission. | certain |
| 21 | **P1** | `docs/backend-persistence.md:11`; `docs/backend-multi-gateway.md:22` | `backend/app/db/models/account.py:21-23` | `accounts` columns are ``id`, `name`, `ibkr_account`, `total_margin`, `enabled`` (both docs, identical list) | Also `default_symbol_limit: Mapped[Decimal | None] = mapped_column(Numeric(18,4), nullable=True, default=None)`. Added by migration `e9f2a7b4c610`, which `backend-persistence.md:53` lists. Its ORM default was changed from `Decimal("10000000.0000")` to `None` by `abd604e`. | **Doc.** A missing column would be P3 — except this is the column that decides whether RMS check 8 rejects (#11), and there is now no documented way for an operator to learn that setting it is what unblocks a symbol. | certain |
| 22 | **P2** | `docs/backend-execution.md:29`; `docs/safety.md:22`, `:63-70`; `AGENTS.md:67`; `README.md:53`; `docs/backend-api.md:17`; `docs/backend-persistence.md:96`; `docs/EC2_OPERATIONS_GUIDE.md:118`; `docs/backend-testing.md:104` | `backend/app/api/routes/webhooks.py:59-63` | Nine separate places say the webhook writes a raw JSON capture per signal to `backend/data/tradingview_webhooks/`. `safety.md:63`: "Every webhook writes a raw JSON capture to `backend/data/tradingview_webhooks/` and nothing removes them, so the directory grows for the life of the host", with a `prune_webhook_captures.py` retention runbook. | `_save_raw_capture_file` (`:59`) has **zero callers** (`rg -n _save_raw_capture_file` returns only its own `def`). `capture_data` is built at `:223` and passed into `create_job_if_not_exists(capture_data=...)` at `:256` — it lands in Postgres, not on disk. The only file written is `incoming_signals.csv` (`:127-141`), explicitly marked `TEMPORARY` at `:30`. `scripts/prune_webhook_captures.py:46` globs `webhook_*.json`, so the documented retention job is a permanent no-op — while the file that *does* grow without bound is undocumented and unpruned. | **Doc**, with a dead-code contribution from the code. During an incident an operator will hunt for a raw payload on disk and find nothing; the payload is in `signal_jobs.capture_data`. | certain |
| 23 | **P2** | `docs/backend-execution.md:56`, `:62-67`; `docs/safety.md:26` | `backend/app/api/routes/webhooks.py:241-246`, `:298-304`; `backend/app/schemas/webhook.py:9` | "**Legacy inline path:** synchronous `process_signal_execution` in the webhook handler only when `worker_pool is None` **and** `session_factory is None` (tests)", plus a three-value response table including `rejected` and `rejected_by_rms`. | No inline path exists: `rg -n "worker_pool is None"` returns nothing, and `session_factory is None` raises HTTP 500 (`:242-246`). The handler returns `status="accepted"` and nothing else (`:299`). `rg -n rejected_by_rms` returns **zero** hits repo-wide; `rejected` is never returned either — parse failures are HTTP 400 (`:188`, `:198`). | **Doc.** Two of three documented response statuses are unreachable, and the documented fallback execution path was deleted. | certain |
| 24 | **P2** | `AGENTS.md:6`; `docs/gaps.md:3`; `docs/backend-map.md:173`; `docs/EC2_OPERATIONS_GUIDE.md:82`, `:407`; root `/home/tradingapp/AGENTS.md:11` | filesystem / git | Six artifacts link to `Execution_System_Architecture.md` as the target-architecture document. `docs/gaps.md:3` bases its entire framing on it: "comparison to [`Execution_System_Architecture.md`](../../Execution_System_Architecture.md)". `docs/EC2_OPERATIONS_GUIDE.md:82` shows it in the directory map. | The file does not exist on the host or anywhere in git history. Only `docs/README.md:11` is honest about it: "(parent of repo, not in git tree)". | **Doc.** `gaps.md`'s "vs target architecture" table cannot be checked by any reader, and the RMS numbering it depends on ("checks 5, 6, 9 remain unbuilt", "the architecture doc reserves check 1 for broker margin") has no verifiable source. | certain |
| 25 | **P2** | `docs/backend-map.md:167`; `README.md:96` | `alembic/versions/h2i3j4k5l6m7_allocation_pair_max_allocation_pct.py:159` | "Chain ends at revision **`a1b2c3d4e567`** (`basket_critical_recovery.py`, revises `f4a8c2d1e903`)" and "Alembic head `a1b2c3d4e567`" | Head is `h2i3j4k5l6m7`. Four revisions follow `a1b2c3d4e567`: `g1h2i3j4k5l6` (users) → `m1n2o3p4q5r6` (margin_rates) → `n2o3p4q5r6s7` (margin_settings) → `h2i3j4k5l6m7` (pair_max_allocation_pct). Nothing revises `h2i3j4k5l6m7`. `docs/backend-persistence.md:33` states the correct head, so the doc set disagrees with itself. | **Doc.** Anyone reconciling migration state or writing a new revision against `a1b2c3d4e567` creates a branched chain. | certain |
| 26 | **P2** | `README.md:96`, `:129` | `backend/app/db/models/*.py` | "PostgreSQL (15 tables…)" and "15 tables (`accounts`, `strategies`, `allocations`, `baskets`, `orders`, `executions`, `positions`, `signals`, `signal_jobs`, `execution_claims`, `kill_switch_operations`, `broker_positions`, `instruments`, `execution_settings`, `events`)" | 20 tables. The list omits `per_symbol_limits`, `margin_rates`, `margin_settings`, `position_reconcile_runs`, `users`, and names a table **`events`** that does not exist — the real table is `event_log` (`backend/app/db/models/event.py:20`, `__tablename__ = "event_log"`). | **Doc.** A wrong table name in the portal's database section is the kind of thing that gets pasted into a query during an incident. | certain |
| 27 | **P2** | `README.md:3` | `git log -1` | "**Current codebase:** commit `acdd451` (2026-08-28)" | HEAD is `259739b` (2026-09-02). Nineteen commits later, including all five patches in §0.1. | **Doc.** The portal self-certifies a commit that predates authentication, systemd management and the RMS fail-closed change. | certain |
| 28 | **P2** | `docs/backend-map.md:16-24`, `:60`, `:62-70` | `backend/app/api/deps.py`, `backend/app/api/routes/`, `backend/app/services/` | Package tree: `deps.py # get_oms / get_order_manager from app.state`; `router.py # mounts orders + config under /api/v1`; `routes/` lists only `health.py`, `webhooks.py`, `orders.py`, `config.py`; `services/` lists 8 modules | `deps.py` is now primarily JWT auth (`oauth2_scheme:24`, `get_token_from_request:39`, `get_current_user:56`, `require_admin:112`). `routes/` also has `auth.py`, `baskets.py`, `emergency.py`, `margin.py`, `reconcile.py`, `service_control.py`, `system_monitor.py`. `services/` also has `watchdog/`, `account_margin.py`, `margin_scanner.py`, `critical_recovery.py`, `system_monitor_service.py`, `reconcile_service.py`, `broker_flatten_service.py`, `position_close_service.py`. | **Doc.** The "where do I change X" map for agents omits the auth layer and 7 route modules; `:73` asserts "Every package above contains runtime code", which reads as completeness. | certain |
| 29 | **P2** | `docs/backend-map.md:91-113` (lifespan); `AGENTS.md:68`; `docs/backend-execution.md:84-98` | `backend/app/main.py:85-182` | Three numbered startup sequences. The most detailed (`backend-map.md:93-108`) is: logging → build → hydrate → connect → hydrate_live_pnl → app.state → RecoveryManager → WorkerPool → PositionReconciler | Real order (MAP §1.4): hydrate (`:85`) → wire critical recovery (`:94`) → TWS connect (`:105`) → hydrate live P&L (`:115`) → `RecoveryManager.run_startup_recovery` (`:138`) → **startup margin scan (`:151`)** → worker pool (`:165`) → reconciler (`:177`) → **enqueue critical baskets (`:182`)**. The startup margin scan, `AccountMarginService` (`:119`), `MarginScanner` (`:142`) and `LivePnlService` (`:79`) appear in no lifespan list, and none of the three `app.state` tables lists them. | **Doc.** Three different renderings of one sequence, none complete. | likely |
| 30 | **P2** | `docs/backend-map.md:179-188` | `backend/app/core/config.py:51-55` | Section titled "## Hardcoded constants (not Settings)" whose table includes `| Gateway limiter defaults | core/config.py | 30/24/6 msg/sec, 8s wait, 2s Error 100 cooldown |` | The values are correct, but they are `Settings` fields (`ibkr_gateway_max_msg_per_sec` etc.), i.e. env-overridable — the exact opposite of the section title, and `docs/backend-config.md:21-26` documents them as env vars. | **Doc.** An operator who needs to tighten pacing during an Error 100 incident is told it requires a code change. | certain |
| 31 | **P2** | `docs/backend-testing.md:23-81` | `backend/tests/**` | "## Test files (48) → intent", presented as the test inventory | `find backend/tests -name 'test_*.py'` returns **95**. Forty-seven are unlisted, including the entire watchdog suite (11 files), `test_auth.py`, `test_authorization_isolation.py`, `test_service_control.py`, `test_risk_ceiling_2000.py`, `test_default_symbol_limit.py`, `test_whatif_probe.py`, `test_margin_{band,check,gate,scanner,tally}.py`, `test_critical_recovery.py`, `test_concurrency_risk.py`, `test_pair_budget_sizing.py`. The doc lists `rms/test_rms_*.py` but the model-market-value test is actually `rms/test_model_market_value.py`. | **Doc.** Also `:38` credits `test_basket_retry.py` with "submit pacer", a class `docs/backend-multi-gateway.md:61` says was removed. | certain |
| 32 | **P2** | `docs/backend-persistence.md:103` | `backend/app/services/system_monitor_service.py:18` | "**Main trading package `backend/app/`:** no `redis` imports (verified by search)." Repeated at `AGENTS.md:78` ("Redis is used only by `demo_streaming`, not by the main trading app") and asserted in `docs/review/MAP.md:24-26`. | `from redis.asyncio import Redis` is a **module-level** import in `backend/app/services/system_monitor_service.py`, reachable from the trading app via `GET /api/v1/system-monitor` (`system_monitor.py:12`). `backend/app/services/watchdog/health.py:697` imports it lazily. `redis>=5.2.1` is a hard dependency in `pyproject.toml:17`. | **Doc.** Redis is not on the *order* path — which is the useful claim — but it is imported by the trading process, so a missing/broken `redis` package breaks a trading-app route. The blanket phrasing is what is wrong. | certain |
| 33 | **P2** | `docs/backend-rms-oms.md:149-155` | `backend/app/services/order_manager.py:186-187`; `backend/app/oms/coordinator.py:172` | Execution-settings table gives `square_off_after_sec | 30 | Fill wait timeout` as the fill-wait value | `BasketCoordinator` is constructed with `fill_timeout=90.0` and `cancel_timeout=30.0` (`order_manager.py:186-187`). `fill_timeout` is later overwritten from `execution_settings` (`coordinator.py:172`); `cancel_timeout` is **not** and stays 30.0 s regardless of operator config. Neither 90.0 nor the non-overridable `cancel_timeout` is documented. | **Doc.** The window between "start of fill wait" and "cancel timed out" matters when reasoning about a half-filled pair. | likely |
| 34 | **P2** | `docs/archive/production_mft_ibkr_pacing.md:10`, `:32-33`, `:44-52`, `:84-89` | `backend/app/broker/ibkr/gateway_rate_limiter.py:15-27` | A second, conflicting pacing design note: "max 40 order messages/sec", "Max Rate: 40 ops/sec", "Max Concurrent Requests: 10", `class IBKRExecutionScheduler` with `max_rate_per_sec: float = 40.0`, and a priority mapping of P0 = `cancelOrder`, P1 = compensation `placeOrder`, P2 = OPEN/CLOSE, P3 = contract details | `IBKR_DOCUMENTED_CEILING_MSG_PER_SEC = 50.0`; configured ceiling 30.0 / normal 24.0; no concurrency cap. `IBKRExecutionScheduler` was deleted. Real priorities: P0 `EMERGENCY_FLATTEN`, P1 order execution (`placeOrder` **and** `cancelOrder` both via `_order_priority`), P2 contract details, P3 market data, P4 diagnostic. The doc's own code sample does not parse (`async def _acquire_token((self)`, `:68`). | **Doc.** Two pacing design notes in one tree with different numbers and an inverted priority ladder. Only `docs/archive/README.md:12` says not to link it, and nothing in the file itself says so. | certain |
| 35 | **P2** | `backend/POSTMAN_API_TESTING_GUIDE.md:5`, `:11` | `backend/app/core/config.py:20-25`; `backend/app/api/router.py` | "**Default Base URL:** `http://127.0.0.1:8000`" and "The application supports two execution modes controlled by the `BROKER_MODE` configuration setting." | `:8000` serves only `/health` and `/api/webhooks/tradingview`; the API lives on `:8001`. `BROKER_MODE` does not exist. Per `docs/backend-api.md:74` this file also documents removed schemas (`SignalSchema`, `PositionSchema`, `MarginSchema`, `PlaceOrderRequest`, `ModifyOrderRequest`, `BrokerStatusResponse`). | **Doc.** Five other documents warn that this file is stale; **the file itself carries no banner**, unlike `backend/docs/DEVELOPER_EXECUTION_GUIDE.md:3` which does. A reader who arrives by search gets `BROKER_MODE` presented as fact. | certain |
| 36 | **P2** | `docs/EC2_OPERATIONS_GUIDE.md:296-303` | `backend/app/db/models/position.py:19`; `backend/app/services/order_manager.py:193-205` | §8 "What the EC2 backend actually does": "Open trades: **in-memory** `PositionBook` — **lost on uvicorn restart**"; "RMS open-position count is in-memory; a restart **resets** it (last incident: 10/10 `OPEN_POSITION_LIMIT` rejected live TradingView OPENs)"; "`app/db` on that commit is incomplete — do not import it on EC2" | Open trades are rows in `positions` (composite PK `(account_id, trade_id)`, `position.py:21-24`). `hydrate_runtime_from_db()` (`order_manager.py:193`) rebuilds `processed_signals` and per-account open-position counts from Postgres on every start. There is no `PositionBook` class. | **Doc.** Self-flagged as a dated snapshot at `:5`, but §8 is written in the present tense and describes a specific past incident as current behaviour. | certain |
| 37 | **P3** | `README.md:21` | `backend/app/rms/engine.py:32-39` | Mermaid node `RMS[RMS 2/3/4/7/8]` | Seven checks run: 1 (MARGIN), 2, 3, 4, 7, 8, 101. The same file says `1/2/3/4/7/8/101` at `:57`. | **Doc**, internally inconsistent. | certain |
| 38 | **P3** | `README.md:24`; `docs/backend-map.md:81` | `backend/app/core/config.py:46`; `scripts/process_manager.py:127`; `backend/app/services/watchdog/config.py:42` | Diagram nodes hardcode the socket as `TWS/Gateway :7497` and `connect_and_start :7497` | Three different ports are in play: `Settings.ibkr_port` default 7497, `process_manager.GATEWAY_API_PORT = 4002`, `WatchdogSettings.gateway_port` default 4001. Which is authoritative is not stated in code (MAP §8.5) or in any doc. | **Doc**, though the underlying disagreement is a code/config problem — see §3.4. | certain |
| 39 | **P3** | `README.md:121` | `backend/app/api/routes/config.py` | "`config/*` (17 endpoints)" | 19 routes are mounted under the `config` router (`config.py:68`–`:809`). | **Doc.** | likely |
| 40 | **P3** | `docs/backend-concurrency.md:31-35` | `backend/app/services/worker_pool.py:35-47` | Five-step idempotency-key derivation | Accurate step for step. One undocumented branch: when `signal_id` is still empty, `:43-44` substitutes `f"SIG-{uuid.uuid4().hex[:12].upper()}"`, so a payload with no `trade_id`/`signal_id` gets a **fresh key on every delivery** and is not deduplicated. | **Doc.** Small omission, but it is the one input that defeats the dedup barrier. | certain |
| 41 | **P3** | `docs/backend-execution.md:164`; `docs/safety.md:73`; `docs/backend-rms-oms.md:131` | `backend/app/broker/ibkr/tws_client.py:410`; `backend/app/services/account_margin.py:302`; `backend/app/oms/ibkr_adapter.py:335` | Limiter coverage listed as "`placeOrder`, `cancelOrder`, and `reqMktData` (P3 try_acquire)" (`backend-rms-oms.md:131` adds `reqContractDetails` P2) | Also paced: `reqAccountSummary` via `blocking_acquire` (`account_margin.py:302`) and the what-if probe at `PRIORITY_DIAGNOSTIC` (P4) (`ibkr_adapter.py:335`). Genuinely unpaced: `reqOpenOrders` / `reqExecutions` (`ibkr_adapter.py:463-481`) and `reqPositions` — which `docs/backend-multi-gateway.md:58` states correctly. | **Doc.** Incomplete rather than wrong. | certain |
| 42 | **P3** | `docs/watchdog.md:120-125` | `backend/tests/` | Five watchdog test files listed | Eleven exist: also `test_watchdog_hardening.py`, `test_watchdog_pre_step9_fixes.py`, `test_watchdog_production_fixes.py`, `test_watchdog_readiness_fix.py`, `test_watchdog_safety_spam_fix.py`, `test_watchdog_semantics.py`. | **Doc.** | certain |
| 43 | **P3** | `docs/EC2_OPERATIONS_GUIDE.md:85` | filesystem | Directory map entry `│   ├── Readme.md` | The file is `README.md`. `docs/README.md:76` claims this casing bug was already fixed ("Fixed `Readme.md` → `README.md`") — it was fixed in `docs/README.md` only. | **Doc.** | certain |
| 44 | **P3** | `docs/backend-persistence.md:65-79` | `backend/app/db/repositories/` | Repository/method table | `SignalJobRepository` omits `update_status`'s fencing parameters and `SignalRepository` omits methods used by `demo_streaming`. Directionally right; not a complete method inventory despite reading as one. | **Doc.** | speculative |
| 45 | **P3** | `/home/tradingapp/pair-allocation-and-model-market-value-spec.md:47-48` | `backend/app/services/model_blue/sizer.py:1-17` | "The current code does something different. `sizer.py`'s docstring says *"first leg is the capital anchor"*, *"base target notional = committed"*" | The docstring now reads "leg target notional = pair_budget * abs(weight)" and "pair market value therefore equals pair_budget" (`:10-11`). The spec's Part A steps 1–2 are also built (`strategy.py:54` column, migration `h2i3j4k5l6m7`). | **Doc** (spec is a completed build plan still written in the imperative). Harmless while it sits outside the repo; misleading if anyone re-executes it. | certain |
| 46 | **P3** | `scripts/process_manager.py:1-45` | `git show 82252f1` | A 45-line module docstring presenting the supervisor as the live design ("Run this as the foreground process under systemd … it IS the supervisor") | `82252f1` is titled *"deprecate process_manager in favor of systemd-native service management"*. The docstring carries no deprecation notice, and `AGENTS.md:55-60`, `README.md:142` and `docs/watchdog.md:11` still present it as the operational path. | **Code comment.** The most-read description of a deprecated component does not say it is deprecated. | likely |

---

## 2. Trust rating per document

### Safe to rely on

| Document | Notes |
|---|---|
| `docs/review/MAP.md` | The only artifact that survived spot-checking essentially intact. One over-broad claim: §0 "There is no Redis import anywhere under `backend/app/`" (see #32). |
| `docs/backend-concurrency.md` | The strongest doc in the tree. Idempotency derivation, lease/heartbeat/reclaim dispositions, claim states and exceptions, the `ACTIVE_LEASE_STATUSES` invariant and all eight "do not break" rules verified correct. Only #40. |
| `docs/OPERATOR_TELEGRAM_ALERTS.md` | Newest doc; alert semantics, session window and severity ladder all check out. Single stale number: gateway `:4002` (#14). |

### Partially stale — usable with the listed corrections

| Document | Verdict |
|---|---|
| `docs/backend-rms-oms.md` | Check ordering, band classifier, basket state machine, paper-retry ports and the `managedAccounts` gate are all correct — genuinely good. Undermined by #11: the two RMS checks that changed behaviour four days ago. |
| `docs/backend-config.md` | Excellent for the 44 fields it covers, including notes finer than the code comments. Fails on the three it omits (#4) — and those are the auth secrets. |
| `docs/backend-persistence.md` | Migration chain complete and in correct order; head correct; in-memory-vs-durable table correct. Fails on #20, #21, #22, #32. |
| `docs/safety.md` | The pacing section is the best writing in the repo — `:77`'s "priority does not isolate probes from orders" is a subtle claim that verifies exactly against `_try_consume_locked`, and the auto-reconnect call-out at `:90` is right. Fails on #17, #22, #23. |
| `docs/backend-execution.md` | Log-grep table is accurate and immediately useful. The live-path diagram is not (#18, #19, #22, #23). |
| `docs/backend-map.md` | Useful as a package index. Wrong on Alembic head (#25), auth (#28), lifespan (#29) and the constants section's own title (#30). |
| `docs/backend-api.md` | Every documented route exists with the documented method and status. It is the *omissions* that make it unsafe (#12, #13). |
| `docs/backend-kill-switch.md` | Verified correct in detail: armed-vs-cleared, the armed-status list, semaphore 5 (`kill_switch.py:149`), fail-closed emergency auth, and every sidecar claim (client id 99, `--pace 0.2`, dry-run default, paper-port refusal, `--client-id` collision refusal). The best runbook here. |
| `docs/gaps.md` | Individually accurate; structurally broken because its comparison target does not exist (#24), and #17's frontend row is now wrong. |
| `docs/watchdog.md` | Rich and mostly right on state machine, notification priority and budget persistence. Wrong on the two things an operator needs at 3am: which port and who owns restarts (#14, #15). |
| `docs/backend-testing.md` | Commands, pytest config and the test-DB isolation explanation are correct. The inventory covers half the suite (#31). |
| `AGENTS.md`, `README.md` | Correct on the invariants they state; the problem is silence about auth and the systemd deployment, plus #22, #26, #27, #37, #38. |

### Actively misleading — reading these is worse than reading nothing

| Document | Verdict |
|---|---|
| **`backend/.env.example`** | Findings #1 and #3. It is not stale prose — it is a template that gets **copied into production** and that tells the reader orders are mocked while omitting the JWT and webhook secrets. Highest-consequence artifact in the tree per byte. Fix before anything else; do not delete (something has to be the template). |
| **`docs/README.md` §"Doc inventory (accuracy vs code)"** | Finding #16. The index and router half is fine. The accuracy table is the failure mode this audit exists to catch: it launders eleven stale documents as verified. **Delete the accuracy table and the two changelogs**, keep the router. An accuracy claim with no date and no re-verification mechanism will always drift into a lie. |
| **`docs/EC2_OPERATIONS_GUIDE.md`** | Findings #5, #6, #36, #43. Its own banner admits three staleness items, which is exactly what makes it dangerous: the banner buys credibility for §5, §6, §8, §9 and §11, all of which are wrong. §11's pre-order checklist and §6's start procedure are both actively harmful. **Recommend: cut to the parts that are still true (SSH, IBC/Jts paths, secret file locations, the never-do list) and delete §5, §6, §8, §9, §11 outright rather than patching them.** |
| **`backend/POSTMAN_API_TESTING_GUIDE.md`** | Finding #35. 847 lines of an API that does not exist, with `BROKER_MODE` as its organising concept and **no in-file warning**. Five other documents warn about it, which only helps readers who arrive via those documents. **Recommend deletion.** If it must be kept for history, move it under `docs/archive/` and add the banner that `DEVELOPER_EXECUTION_GUIDE.md:3` already has. |
| **`docs/archive/production_mft_ibkr_pacing.md`** | Finding #34. A competing pacing spec with a different ceiling (40 vs 30/50), a non-existent class, an inverted priority ladder and code that does not parse. Its harm is proportional to how plausible it looks to someone grepping for "pacing". **Recommend deletion**; `backend-multi-gateway.md` already carries the surviving intent. |
| **`docs/backend-multi-gateway.md`** — as-is sections only | Findings #7, #8, #9, #10. The target/phase/open-questions half is thoughtful and worth keeping; §"As-Is" is the most dangerous prose in the repo because it declares three implemented safety mechanisms absent, and contradicts itself on two of them within the same file. **Recommend: delete the "As-Is" tables and link to `backend-execution.md` / `backend-rms-oms.md` instead of maintaining a third copy.** The gap-analysis table at `:325-342` should be regenerated from scratch, not edited — roughly a third of its rows describe built features. |

---

## 3. Code defects surfaced by this audit

These are places where the documented intent looks right and the implementation looks wrong.
They are bugs, not doc bugs.

### 3.1 Webhook authentication fails open when the secret is unset — P0

`backend/app/api/routes/webhooks.py:147-160`

```python
if not settings.webhook_auth_enabled:
    ...
    return
expected_secret = settings.webhook_auth_secret
if expected_secret:
    incoming_secret = request.headers.get("X-Webhook-Secret")
    ...raise 401
```

With the shipped defaults — `webhook_auth_enabled=True`, `webhook_auth_secret=None`
(`config.py:110-111`) — the function returns without checking anything, logs nothing, and the
endpoint accepts any request. The sibling endpoint in the same app implements the opposite
policy and documents it: `emergency.py:29-32` *"Fails closed (401) if secret is unconfigured
when enabled"*, enforced at `:40-47`. Two authentication gates on one service, opposite
semantics, and the divergence is invisible in the docs (#2).

Suggested direction: mirror `emergency.py` — reject when enabled-and-unconfigured, or refuse
to start. A startup assertion is preferable to a per-request 401 for a webhook, since
TradingView failures are silent to the operator.

### 3.2 The RMS "fail-closed symbol check" is unreachable on the live path — P1

`abd604e` intended, per its own message, to *"enforce strict $2,000 risk ceiling, fail-closed
symbol & position checks, and remove $10M default"*. It removed the `$10M` ORM default from
`accounts.default_symbol_limit` (`db/models/account.py:22`) and from
`schemas/config_schemas.py:42`, and added the reject branch at
`rms/checks/money_per_stock.py:60-66`. But it did not touch
`backend/app/services/order_manager.py:150` and `:1448`:

```python
money_limit_per_symbol=Decimal(10_000_000),
```

`_ensure_strategy_config` (`:1438-1456`) creates a `StrategyConfig` with that value whenever
one is absent, and it is called unconditionally at `:736` for every fan-out account, before
`build_intent`. Check 8 resolves `limit_per_symbol = account_limit if account_limit is not
None else strategy_limit` (`money_per_stock.py:59`), so `strategy_limit` is **never** `None`
on the live path. Consequences:

- the `NO_SYMBOL_LIMIT_CONFIGURED` branch is dead in production;
- the effective per-symbol cap for an unconfigured symbol is **$10,000,000**, not a rejection;
- `_ensure_strategy_config` only ever propagates `max_open_positions` from the DB
  (`:1451-1456`), never `money_limit_per_symbol`, so the 10M value cannot be lowered
  except by setting a per-account or per-symbol limit.

The docs are silent on all of it (#11, #21), so the gap is invisible from either side. Check 7
does not have this problem: its account override comes from `ctx.max_open_positions` via
`order_manager.py:737-739`, so its reject branch is reachable.

### 3.3 `IBKR_GATEWAY_EMERGENCY_RESERVE_PER_SEC` is a knob with no effect — P2

`backend/app/broker/ibkr/gateway_rate_limiter.py:57`, `:69-70`, `:78`

The parameter is accepted, validated (`>= 0`), stored on the instance, and threaded all the
way from `Settings.ibkr_gateway_emergency_reserve_per_sec` (`config.py:53`) through
`main.py:49`. It is then **never read again** — `rg -n emergency_reserve` shows no use in
`_try_consume_locked`, `_refill_locked` or `_seconds_until_available_locked`. The actual
emergency reserve is implicit: `max_msg_per_sec - normal_msg_per_sec = 30 - 24 = 6`, which
coincides with the default and hides the problem.

Setting `IBKR_GATEWAY_EMERGENCY_RESERVE_PER_SEC=12` changes nothing. Setting it to `0` does
not remove the reserve. `docs/backend-config.md:23` describes it as "P0 flatten reserve
(within global ceiling)" — a reasonable reading of the intent, and untrue of the code.
Either derive it (`normal = max - reserve`) or delete the parameter; a config knob that
silently does nothing on an emergency path is worse than no knob.

Related: `IBKR_DOCUMENTED_CEILING_MSG_PER_SEC = 50.0` (`:15`) is also defined and never used.
Harmless, but it means the 30-vs-50 headroom relationship the docs describe is not enforced
anywhere — nothing stops `IBKR_GATEWAY_MAX_MSG_PER_SEC=80`.

### 3.4 Three components disagree about the gateway port, with none authoritative — P2

- `Settings.ibkr_port` default `7497` (`config.py:46`) — the socket the app actually opens
- `process_manager.GATEWAY_API_PORT = 4002` (`scripts/process_manager.py:127`) — the port the
  supervisor waits for before starting the trading app
- `WatchdogSettings.gateway_port` default `4001` (`watchdog/config.py:42`) — the port the
  watchdog health-probes, changed from 4002 by HEAD commit `259739b`

4002 is IB Gateway **paper**; 4001 is IB Gateway **live**. This is not only a monitoring
concern: `paper_retry_ports_allowed` (`oms/retry_policy.py:11`) keys incomplete-leg basket
retries off `Settings.ibkr_port`, so the port also decides whether auto-retry and auto
square-off are enabled. A `.env` that sets `IBKR_PORT=4001` silently disables basket retries
while the watchdog reports healthy and the supervisor waits on 4002. MAP §8.5 records the
disagreement as unresolvable from the code; no doc states which value is authoritative (#38).

### 3.5 Dead capture path with a documented retention job that cannot fire — P3

`_save_raw_capture_file` (`webhooks.py:59-63`) has no callers, so
`scripts/prune_webhook_captures.py:46`'s `capture_dir.glob("webhook_*.json")` matches nothing
— a scheduled retention job that will report success forever while the file that actually
grows without bound, `incoming_signals.csv` (`webhooks.py:127-141`), is never pruned. Either
call the function or delete it and repoint the pruner at the CSV (#22).

### 3.6 `GET /health/ready` never signals not-ready — P3

`backend/app/api/routes/health.py:20-40` returns HTTP 200 with
`{"status": "degraded", ...}` on TWS disconnect and on any exception, with the inline comment
*"if disconnected, still return degraded but not 500 — readiness reflects it"*. This is a
deliberate choice, and `docs/watchdog.md:24` documents the watchdog inspecting the body. But
it means the endpoint is unusable as a readiness probe for anything that keys off status code
— systemd, a load balancer, or `curl -f`. Worth an explicit note next to the route rather
than leaving it to a comment.

---

## 4. Documentation gaps — undocumented but shouldn't be

Ordered by consequence.

1. **Authentication and authorization — entirely undocumented.** No document describes:
   JWT bearer auth (`deps.py:56`); the admin role gate (`deps.py:112`) and *which* routes it
   guards (`create_account`, `delete_account_api`, `create_account_allocation`,
   `patch_execution_settings`, `patch_margin_settings`, `list_account_margins`,
   `get_system_monitor`, both `service-control` routes); `jwt_access_token_expire_minutes=480`
   (an 8-hour admin token); the `users.role` model; or how `users` rows are created — MAP §2.3
   and §8.6 confirm nothing in the application or any migration inserts one. Two further
   behaviours need documenting because they are security-relevant and surprising:
   - **`?token=` query-parameter auth** (`deps.py:42`, `:47`). A full-privilege access token
     in a URL lands in access logs and is forwarded through the `:8010` proxy.
   - **`TRADINGAPP_TESTING=1` promotes any unauthenticated request to a synthetic admin**
     (`deps.py:67-75`, and again at `demo_streaming/api.py:69-77`). The variable is also read
     by `db/session.py:21`, `main.py:82` and `config.py:142`. `config.py:142-148` guards the
     *database*, but nothing guards the *auth bypass* — set it in a systemd
     `EnvironmentFile` against a non-production DB name and the whole trading API is open
     admin. `docs/backend-config.md:60` mentions only the DB guard.

2. **A gateway-failure runbook.** The brief's premise. Nothing tells an operator what to do
   when the socket drops. The facts are established across three docs but never assembled:
   `on_connection_closed` marks every non-terminal in-memory OMS order `ERROR` without calling
   `reqOpenOrders` first (`ibkr_adapter.py:1017`), so orders may still be live at IB while the
   ledger says failed; there is no reconnect (`safety.md:90` says so); `submit_order` raises
   `ConnectionError` (`:386-389`); and recovery is operator-driven. What is missing is the
   procedure: how to tell a live IB order from an ERROR-ed local one, when it is safe to
   resubmit, and which of `POST /reconcile/positions/flatten`,
   `POST /config/accounts/{id}/square-off` and `scripts/oms/flatten_gateway_positions.py`
   to reach for. `backend-kill-switch.md:131` distinguishes the last two well; nothing covers
   the first minute of the incident.

3. **Rate-limiter behaviour on restart.** Buckets are initialised **full** —
   `_global_tokens = float(burst_cap)` where `burst_cap = max_msg_per_sec = 30`
   (`gateway_rate_limiter.py:82-88`). A process restart therefore grants an immediate 30-message
   burst, and a crash-loop can issue a 30-burst per restart with no memory of the previous one.
   Nothing is persisted; nothing is documented. Burst size is undocumented generally: global
   burst 30, normal burst `min(30, 24) = 24` (`:85-86`), neither stated anywhere.

4. **Identifier normalization rules.** `core/identifiers.py` supplies
   `normalize_strategy_id` (lowercases) and `normalize_trade_id` (preserves case), and the
   asymmetry feeds the dedup hash. `backend-concurrency.md:31-32` notes it in passing and
   `:232` warns that changing it needs a backfill, but no document states the rules, why they
   differ, or which columns they are applied to. `_validate_ibkr_account` also upper-cases
   account codes (`ibkr_adapter.py:159`) — a third convention, undocumented.

5. **What the dedup barrier does *not* cover.** `backend-concurrency.md` documents
   `execution_claims` well, but three limits go unstated: the ingest idempotency key is not
   account-scoped, so one job fans out to N accounts under one key; the empty-`signal_id`
   fallback generates a fresh UUID per delivery and defeats dedup entirely (#40); and per
   MAP §7.3 **no test in the suite references `execution_claims`, `ExecutionClaimRepository`
   or `dedupe_key`** — the durable barrier the code calls "authoritative"
   (`execution_claim.py:1-8`) is untested, including all three of its exception types.

6. **Lease recovery under test.** MAP §7.3 records that `heartbeat_lease`,
   `reclaim_stale_jobs`, the fenced-write path and all three lease-expiry dispositions
   (requeue / quarantine / dead-letter) have no coverage. `backend-concurrency.md:79-86`
   documents the behaviour as settled fact without noting it is unverified.

7. **The systemd deployment.** Fourteen unit files in `deploy/systemd/` and no document
   describes them as a set: unit names, which are `enable`d vs timer-driven
   (`ibgateway.service` is intentionally not in `multi-user.target`), the
   `trading-session-{start,stop}.timer` window, the `.path`-trigger restart chain via
   `storage/state/restart_backend.trigger`, or `scripts/ibgateway-wrapper.sh` and
   `scripts/backend-ready-trigger.sh`. `docs/watchdog.md:100-108` covers three units (#15);
   `docs/EC2_OPERATIONS_GUIDE.md` describes the tmux predecessor (#6).

8. **`POST /api/v1/service-control/{service}/{action}`.** An admin HTTP route that shells out
   to `systemctl` via `subprocess` (`service_control.py:7`) against a four-unit allowlist
   (`:20-26`) and four actions (`:29`). It is reachable unauthenticated-at-that-layer through
   the `:8010` proxy (#13). It appears in no API document, no safety document and no runbook.

9. **`kill_switch_operations` schema and the `UNRESOLVED` disposition.**
   `backend-kill-switch.md` documents the status machine and the armed-status list correctly,
   but the table's columns appear in no schema doc, and `UNRESOLVED` — "remaining exposure
   after flatten", the outcome an operator most needs a procedure for — has semantics but no
   runbook.

10. **Process-to-DB matrix.** No document contains one; MAP §2.1–2.3 is the only such
    inventory and it is the review artifact, not the doc tree.
    `docs/backend-persistence.md` lists tables and repositories but never says which process
    or code path *writes* each one. The undocumented-writer direction is the dangerous one and
    it is entirely undocumented: `positions` has **six** distinct writers (Model Blue
    persistence, live-P&L tick persistence *from the TWS callback thread*, kill-switch
    flatten, the single-pair close service, the alternate trade-book path, and an out-of-band
    CLI script); Alembic itself writes data to `allocations`, `execution_settings` and
    `margin_settings`; and `backend/scripts/repair_historical_killswitch_positions.py` writes
    both `positions` and `event_log` from outside every runtime process.

11. **`strategies` has no runtime writer.** MAP §2.2 establishes that nothing in the
    application inserts, updates or deletes a strategy row — they arrive by migration or by
    hand — yet the config API reads the table to validate `allocations`. An operator adding a
    new strategy has no documented path, and no document says one is needed.
