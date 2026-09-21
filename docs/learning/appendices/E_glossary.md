# Appendix E — Glossary

## Identifiers

Confusing these has caused real defects; `AGENTS.md` §9 is the authority.

| Term | Type | Scope / lifetime |
|---|---|---|
| `account_id` | BIGINT | Internal surrogate key. **Never sent to IBKR** |
| `ibkr_account` | string (`DU123456`) | The authoritative IBKR account code |
| `internal_order_id` | string (`ORD-…` / `MAN-…`) | Ours; passed to IBKR as `orderRef` |
| `broker_order_id` | int | **Session-scoped** — invalid across gateway restarts |
| `perm_id` | BIGINT | Permanent broker order id; survives restarts |
| `trade_id` | string | Economic position/lot identity. **Never reused once closed** |
| `exec_id` | string | Unique broker fill id. Processed exactly once |
| `con_id` | BIGINT | IBKR contract id. CFD and underlying STK differ |
| `leg_index` / `leg` | int / `L0`,`L1`… | Logical leg identity. **Retries share it** |

## Domain terms

| Term | Meaning |
|---|---|
| **Basket** | The multi-leg unit of execution. Can answer "is this pair complete?" |
| **Leg** | One side of a multi-leg intent, identified by `leg_index` — *not* by row count |
| **Retry** | Resubmission of a leg's **remaining** quantity. New `orders` row, same `leg` |
| **Compensation** | Reverse CLOSE legs unwinding a partially-filled basket. Only ever *reduces* exposure |
| **Naked pair / naked leg** | One side of a pair filled, the other not — the exposure baskets exist to prevent |
| **Red Zone** | Window where non-emergency execution is deferred: before RTH open, `post_open_delay` after it, `buffer_seconds` before close, and all day on non-trading days |
| **Fan-out** | One signal becoming N per-account execution attempts |
| **Cancel Exposure** | Per-account setting. When **off** (default), an incoming pair may not partially cancel an existing paired exposure |
| **Kill switch scope** | `engine`, `manual` or `account`. Armed independently; idempotent within a scope |
| **Flatten snapshot** | Immutable set of positions captured at arming time, so reconciliation works against that set |
| **Rogue trade** | Broker/ledger mismatch persisting across `rogue_confirm_sweeps` sweeps |
| **Ledger ghost** | Ledger has a position the broker does not |
| **Broker orphan** | Broker has a position the ledger does not |
| **Qty drift** | Broker and ledger quantities disagree |
| **Ghost positions (UI)** | Positions still rendered after a flatten was accepted but before convergence |
| **Flapping** | Repeated oscillation within `notification_flapping_window_sec` exceeding `notification_flapping_threshold` |

## Status values

**`signal_jobs`:** `PENDING` → `CLAIMED` → `PROCESSING` → `PROCESSED` /
`REJECTED` / `RECOVERY_REQUIRED` / `QUARANTINED`

`RECOVERY_REQUIRED` = died before emitting orders (retryable).
`QUARANTINED` = died after emitting orders (not safely retryable).

**Basket:** `EXECUTING` → `ACCEPTED` / `UNWINDING` → `COMPENSATED` / `CRITICAL`

**Canonical signal status:** `PROCESSING`, `ACCEPTED`, `REJECTED`,
`SQUARE-OFF`, `EXPIRED`

**Kill-switch operation:** `ACTIVATING`, `FLATTENING`, `RECONCILING`,
`RETRYING`, `FLAT`, `COMPLETE`, `UNRESOLVED`

Armed statuses keep an account blocked. Engine and account scopes stay armed on
`UNRESOLVED`; manual scope does not.

**Audit result:** `PENDING` (intent recorded, outcome unknown), `SUCCEEDED`,
`ACCEPTED` (enqueued, completion async), `PARTIAL`, `REJECTED` (business rules),
`DENIED` (authz), `FAILED` (errored), `UNKNOWN`

## Audit write modes

| Mode | Semantics |
|---|---|
| `record()` | Joins the caller's transaction — atomic with the state change |
| `record_committed()` | Own transaction, best-effort, never raises |
| `operation()` | Intent-first: `PENDING` committed **before** the side effect, finalized once after |

## Processes and ports

| Name | Port | systemd unit |
|---|---|---|
| Webhook ingest | 8000 | `webhook-ingest.service` |
| Trading backend | 8001 | `trading-backend.service` (`SELF_UNIT`) |
| Demo streaming | 8010 | `demo-streaming.service` |
| IB Gateway | 4002 (paper) | `ibgateway.service` |
| Watchdog | — | `watchdog.service` |

Service-control API keys are `ibgateway`, `backend`, `webhook`, `watchdog` —
**not** the unit names.

## Confidence labels

| Label | Meaning |
|---|---|
| **Confirmed** | Verifiable in code, schema, tests or git history today |
| **Reported** | Asserted by a dated document, not independently re-verified |
| **Assumption** | A stated premise the analysis depends on |
| **Unknown** | Explicitly not established by available material |
