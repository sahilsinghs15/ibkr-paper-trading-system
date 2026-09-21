# Appendix A — Complete Timeline

Dated from git history. 224 commits, 2026-08-07 to 2026-09-21.
**Confidence: Confirmed** unless marked otherwise.

## Phase 1 — Prototype (Aug 7–14)

| Date | Event | Commit |
|---|---|---|
| 08-07 | Initial commit | `3a9d00f` |
| 08-08 | Trading system foundation; mock broker | `34f2dfd`, `998f33f` |
| 08-09 | Candle strategy, market data simulator, `OrderManager`, TWS client wrapper | `2e30b58`, `ebfc84f`, `0e742b4`, `3397a11` |
| 08-10 | IBKR broker and market data integration | `6403eb3` |
| 08-12 | Webhook support added to backend API | `25165db` |
| 08-13 | Persistent capture of raw TradingView payloads | `2540028` |
| **08-14** | **Strategy-based broker replaced by OMS + RMS** | `902ca4d` |

## Phase 2 — Durability (Aug 17 – Sep 3)

| Date | Event | Commit |
|---|---|---|
| 08-17 | Alembic introduced | `86485a6` |
| 08-18 | Postgres config; baskets; account strategy routing; execution persistence | `c186d07`, `6bc05f4`, `2c63b3b`, `fd5f0d6` |
| 08-19 | Execution retry logic, kill switch, Model Blue sizing | `d62a8cd`, `d9a9119` |
| 08-20 | CFD→underlying market data; naked-pair basket reconciliation; canonical signal status; test `NullPool` | `4263cb9`, `7815be1`, `8c99ea4`, `4f945ca` |
| 08-21 | **Durable webhook queuing + worker pool**; IBKR pacing; kill-switch durability; repair script | `4c47e37`, `f49a093`, `323d138` |
| 08-24 | Optional webhook auth; production architecture docs | `61571ca` |
| 08-26 | System Monitor page; authenticated emergency kill-switch webhook | `c2915ad`, `c6aad09` |
| 08-27/28 | `process_manager`; decoupled webhook logic | `6704d97`, `acdd451` |
| 08-31 | **Watchdog service**; auth + RBAC + cross-account isolation | `8cbe353`, `28f5325` |
| 09-01 | **systemd-native service management**; watchdog hardening | `394e616`, `82252f1`, `f8a9a91` |
| 09-02 | Market-hours aware lifecycle; strict risk ceiling, fail-closed checks | `6f31919`, `abd604e` |
| **09-03** | **Structured review → `docs/review/` (10 P0, 23 P1)**; whatIf margin sequence; position sizing fixes | `448a9e2`, `a817a47`…`d9bd54a` |

## Phase 3 — Operator control (Sep 8–21)

| Date | Event | Commit |
|---|---|---|
| 09-08 | **Global Red Zone gate + post-session release**; Telegram notifications | `4741de4`, `9b26974` |
| 09-09 | NYSE calendar + startup guards; Notification Center; audit log v1; SL/TGT | `a82cbca`, `05263f2`, `481e9a0`, `c144cea` |
| 09-10 | Trade book sync, idempotent execution upsert; Ingest Feed | `6638176`, `8864e41` |
| **09-11** | **Manual trading system**; account-level trading pause | `837baf2`, `991a9f0` |
| 09-14 | Manual idempotency lifecycle; trade-id reuse prevention; manual live PnL | `d7be7a8`, `2c74e1b`, `00b2315` |
| 09-15 | **Attribution fixes** (KIE→SMH, Qty10/Filled30, close intent); dual-option kill switch; rogue spam | `6130661`, `fe44947`, `b977cad`, `0272637`, `460637e` |
| 09-16 | Kill-switch convergence, locking, engine/manual isolation; flatten verification; engineering docs | `29fce78`, `ffb16bd`, `371727f` |
| **09-17** | **Centralized notification system**; manual kill-switch scope; ~15 suppression commits | `42a36cd`, `05cf74a` |
| **09-18** | **Operator audit trail replaces legacy audit**; RMS net basis; manual workspace | `5b9d094`, `38bc676`, `a9c6273`, `cd2b22a` |
| **09-21** | Compensation during kill switch; manual cache release; signal leg retry grouping; test determinism | `64f3009` |

## Density

| Month | Commits |
|---|---|
| 2026-08 | 94 |
| 2026-09 | 130 |

61 of 224 commit subjects begin with or contain `fix`.

## Reading the timeline

The three phases are visible in what the commits are *about*:

- Phase 1 asks "can we place an order?"
- Phase 2 asks "will we still be correct after a crash?"
- Phase 3 asks "can the operator see and control what happened?"

Each phase's characteristic bug class follows: prototype bugs are functional,
durability bugs are concurrency and restart bugs, operator-control bugs are
truthfulness bugs.
