# Chapter 17 — Data Integrity Fixes

Changes that corrected what the system *believed* about money, positions or
attribution.

## Inventory

| Date | Change | Commit | What was wrong |
|---|---|---|---|
| 2026-08-18 | CFD share sizing to whole lots | `0d688a2` | Fractional CFD sizing |
| 2026-08-18 | Fill precision; execution persistence | `fd5f0d6` | Fills not durably recorded |
| 2026-08-20 | Truthful canonical signal reconciliation; EXPIRED for zero-order stale signals | `8c99ea4` | Signal status not reflecting reality |
| 2026-09-02 | Strict risk ceiling; fail-closed symbol and position checks | `abd604e` | Permissive default admitted oversized orders |
| 2026-09-03 | Major fixes in position sizing | `448a9e2` | Not established in detail |
| 2026-09-09 | Stop/target logic fixes; account-level SL fixes | `c144cea`, `2f8fb13` | Exit level computation |
| 2026-09-10 | Idempotent execution upsert for trade book | `6638176` | Duplicate execution rows |
| 2026-09-14 | Prevent closed trade-id reuse | `2c74e1b` | Reused `trade_id` corrupting lot identity |
| 2026-09-15 | Repair mismatched executions ("Qty 10 / Filled 30") | `fe44947` | Fills attached to wrong position |
| 2026-09-15 | Strict execution correlation prevents KIE→SMH mis-attachment | `6130661` | Fill attributed to wrong symbol |
| 2026-09-15 | Preserve close intent via `trade_id`; prevent new SHORT on CLOSE | `b977cad` | CLOSE opening a new position |
| 2026-09-16 | Account-flatten aggregate execution attribution | `0e9dbc7` | Cross-trade double-count |
| 2026-09-16 | Account-flatten ledger close requires broker-flat verification | `ffb16bd` | Ledger closed without evidence |
| 2026-09-18 | Net per-symbol basis tracks signed quantity at order price | `a9c6273` | Net exposure basis mis-valued |
| 2026-09-21 | Signal tray leg grouping (presentation-level integrity) | `64f3009` | Retries counted as legs |

## The attribution cluster — September 15–16

Six commits over two days all concern one question: **which position does this
fill belong to?**

- `fe44947` — a position showing quantity 10 and filled 30, i.e. foreign fills
  landing on it.
- `6130661` — a fill for KIE attributed to SMH.
- `b977cad` — a CLOSE treated as an opening SHORT.
- `0e9dbc7` — the same execution counted against two trades during an account
  flatten.
- `ffb16bd`, `cf79bfc` — ledger closure without broker-flat verification.

**Confirmed:** these commits exist with these messages, and
`backend/scripts/repair_manual_mismatched_executions.py` exists, establishing
that bad rows reached the database and required retroactive correction.

**Unknown:** the trigger sequence, the number of affected positions, whether any
financial loss occurred, and the diagnosis path. The commit messages state
symptoms; no investigation record survives. A case study is not written for them
because writing one would require inventing the investigation.

### Why this cluster matters

`AGENTS.md` §9 ends with:

> Never correlate entities by symbol alone when stronger identities
> (`con_id`, `trade_id`, `perm_id`) exist.

That rule is not abstract. `6130661` — "strict execution correlation prevents
KIE→SMH mis-attachment" — is what symbol-based correlation produces in a system
where the same symbol can appear on multiple legs, multiple accounts, and both
ledgers at once.

Note also that `0d688a2`'s date (2026-08-18) shows the system had already learnt
that a CFD and its underlying STK are different contracts with different
`con_id`s. Attribution then failed anyway a month later, at a different layer.
**Identity discipline has to hold at every layer, not just the one where it was
last violated.**

## The verification principle

`ffb16bd` — "account flatten ledger close requires broker-flat verification and
execution evidence" — encodes a rule worth stating generally:

**Do not close a ledger row because you asked for something. Close it because
you can see it happened.**

The same idea appears in:

- Chapter 13, where broker recovery must require `broker_connection` ready
  rather than inferring it.
- Chapter 14, where the watchdog distinguishes probe failure from unsafe state.
- Chapter 12, where the audit recorder commits *intent* separately from
  *outcome*, so a request is never confused with its result.

Requesting and observing are different facts, and the system has been bitten
every time it conflated them.
