# Chapter 18 — Operational Fixes

Changes that made the system usable and truthful to the person operating it.

## Inventory

| Date | Change | Commit |
|---|---|---|
| 2026-08-20 | Signal status transitions published without page refresh | `888f1e1` |
| 2026-08-21 | Signal monitor / signal tray data disappearance fixed | `a869a69` |
| 2026-08-26 | Read-only System Monitor page and API | `c2915ad` |
| 2026-09-01 | System monitor with webhook/watchdog and manual service controls | `7c7b893` |
| 2026-09-01 | RESTART control in System Monitor UI | `94ce7b6` |
| 2026-09-09 | P&L hidden when `market_data_status` is UNAVAILABLE | `003c6b9` |
| 2026-09-09 | Accessible 3-state column sorting across tables | `30506d8` |
| 2026-09-10 | Admin-only Ingest Feed for raw `signal_jobs` | `8864e41` |
| 2026-09-16 | polkit rules so the app can manage systemd units | `6df09dc` |
| 2026-09-17 | Strategy Allocations block removed from Account Settings | `82ac7ef` |
| 2026-09-18 | Notification overhaul; toast replay spam eliminated | `9e469cc` |
| 2026-09-18 | Unified Manual Trading workspace with inline ledger | `cd2b22a` |
| 2026-09-21 | Positions resync after kill-switch flatten | `64f3009` |

## Truthfulness over completeness

The clearest operational principle in this history is that **the UI must show
what the system actually knows, including when it knows nothing.**

`003c6b9` (2026-09-09) hides P&L when `market_data_status` is `UNAVAILABLE`
rather than displaying a stale or derived number. `AGENTS.md` §11 generalises
it:

> Frontend components must gracefully handle SSE disconnects and display
> stale-data warnings rather than synthesizing artificial numbers.

An operator makes decisions from this screen. A number that is wrong is worse
than a number that is missing, because a missing number prompts a question and a
wrong number does not.

The same principle drove the notification naming work in Chapter 13
("evidence-based state and canonical terminology") and the kill-switch resync in
Chapter 10 — in each case the display was confidently showing something the
system had not established.

## Alert noise as an operational defect

`9e469cc` (2026-09-18) "eliminate toast replay spam" and the fifteen-commit
suppression sequence of 2026-09-17 treat notification volume as a bug, not a
preference. The reasoning is sound: an operator who learns to dismiss alerts
will dismiss the one that mattered.

`460637e` (2026-09-15) "rogue trade spam fix" is the same problem in the
reconciliation path, and it produced the `rogue_confirm_sweeps` mechanism —
a mismatch must persist across consecutive sweeps before alerting. That is a
better answer than raising the threshold: it distinguishes transient from
persistent rather than simply alerting less.

## Service control

Service management moved from a custom `process_manager` (2026-08-27,
`6704d97`) to systemd-native control (2026-09-01, `394e616`), with
`process_manager` deprecated in `82252f1`. `6df09dc` (2026-09-16) added polkit
rules so the application user can manage the units without broad privilege.

The audit requirement that follows from operator-triggered service control —
that audit rows must be committed **before** the backend restarts itself — is
covered in [Chapter 12](../part2_safety_operations/12_operator_audit.md).

**Note for local development:** the systemd units exist on the deployment host,
not on developer machines. `systemctl` calls fail with "Access denied" locally.
Service-control behaviour is covered by seven mocked tests in
`test_audit_operations.py`; there is no need to invoke the real service manager
to exercise it, and doing so touches the developer's host.
