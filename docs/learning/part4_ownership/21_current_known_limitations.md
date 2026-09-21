# Chapter 21 — Current Known Limitations

What is still wrong, unproven, or unverified. Status as of 2026-09-21.

Entries are labelled with the confidence conventions from
[`README.md`](../README.md).

## Unresolved review findings

The 2026-09-03 review produced 10 P0 and 23 P1 findings. Two have been
re-verified while assembling this documentation. **The rest carry status
Unknown** — they have not been shown to be fixed, and they have not been shown
to still be present.

| Finding | Severity | Original confidence | Status |
|---|---|---|---|
| C1 — kill-switch check-then-act | P0 | certain | **Fixed (Confirmed)** — unique index |
| C11 — heartbeat exception ignores lease loss | P2 | certain | **Fixed (Confirmed)** |
| C2 — close paths bypass `execution_claims` | P0 | certain | **Unknown** |
| C3 — kill switch armed after gate read, before submit | P0 | certain | **Unknown** |
| C4 — OPEN/CLOSE claim ordering defeated by concurrency | P1 | likely | **Unknown** |
| C5 — `RMSContext.margin_commitments` cross-thread RMW | P1 | certain | **Unknown** |
| C6 — blocking `request_contract_details` on the event loop | P1 | certain | **Unknown** |
| C9 — rate limiter does not prioritise orders over market data | P2 | likely | **Unknown** |
| C10 — unbounded callback persist coroutines exhaust the pool | P2 | likely | **Unknown** |
| C13 — `exposure_key` not upper-cased; per-symbol ceiling doubles | P1 | likely | **Unknown** |
| Lifecycle #1 — late `execDetails` regresses a terminal order | P0 | certain | **Unknown** |
| Remaining lifecycle/resilience findings | P0–P3 | mixed | **Unknown** |

**This is the highest-value outstanding work in the repository.** A review of
that quality with no tracked remediation list means nobody can currently say
which P0s are live. Producing that list is a bounded task — each finding names
a file and line range.

## Architectural limitations

| Limitation | Source | Notes |
|---|---|---|
| Single TWS socket shared by all accounts | ADR 2, Risk 2 | One account can starve another via the shared rate limiter |
| Multi-gateway is a target, not current | `docs/backend-multi-gateway.md` | Do not build without a mandate |
| `positions` is Model Blue pair-shaped, not generic N-leg | `position_repository.py` | Noted in code |
| Two position ledgers unified only at read time | ADR 5 | Merging gated on full migration planning |
| In-memory flapping/rogue windows do not survive restart | Chapter 13 | Alerting state is not durable |

## Known operational gaps

- **`scheduleFlattenResync()` is wired only to the kill switch.** Close Pair and
  other 202-returning endpoints may have the same ghost-position behaviour.
  **Not established** — not investigated.
- **Bare `loop.create_task` for kill-switch notifications** (`kill_switch.py`
  ~line 298) does not retain the task. The flatten task *is* retained; this one
  is not. Same shape as finding C7, lower severity. **Confirmed present.**
- **`app.state.client` and `app.state.ibkr_adapter` are not restored between
  tests.** `_restore_trading_app_state` covers only `session_factory` and
  `order_manager`. **Confirmed.**
- **Detail-view audit investigation queries are unprofiled.** Verified against a
  table with fewer than 20 rows. Behaviour at scale **not established**.

## Unexplained

- **`test_concurrent_same_idempotency_exactly_one_placeorder` failed once** in a
  full-suite run on 2026-09-21 and did not reproduce in 20+ isolated runs, 3
  per-file runs, or 4 subsequent full suites. The idempotency barrier in
  `ManualTradingService.submit_order` was re-read and is correct: the
  `manual_orders` insert is committed before `placeOrder()`, so a
  unique-constraint loser cannot place an order.

  **Deliberately left open rather than closed.** It guards a double-execution
  invariant. If it recurs, run with `--tb=line` — both assertions carry
  diagnostic messages (`placed={n} expected 1` and `unexpected exception {r}`)
  that `--tb=no` discards.

## Environment

- Concurrent pytest runs against `ibkr_trading_test` can deadlock on
  `TRUNCATE ... CASCADE` (Risk 1).
- `pyproject.toml` listed `boto3` twice (`>=1.34.0` and `>=1.35.0`) until
  2026-09-21; `uv run` re-derived a duplicate entry into `uv.lock` on every
  invocation. Resolved by keeping the stricter pin. **Confirmed fixed** — the
  lock is now stable across `uv run`.
- `docs/review/*.md` paths are relative to `/home/tradingapp/app` on the
  deployment host, not to a developer checkout.
