# Production MFT Step-by-Step Incremental Migration Plan

This document outlines the 20-phase step-by-step migration plan to upgrade the TradingView → FastAPI → OMS → IBKR system into a production-grade MFT execution architecture without disrupting trading logic or breaking existing test suites.

---

## Migration Roadmap & Step Verification Matrix

| Phase | Migration Step | Key Files / Changes | Verification Step |
|---|---|---|---|
| **Phase 0** | Baseline Audit & Documentation | Created `docs/production_mft_baseline.md` | Audit verified against codebase. |
| **Phase 1** | Target Architecture Definition | Created `docs/production_mft_architecture.md` | Design aligns with decoupling requirement. |
| **Phase 2** | Persistent Signal Inbox Schema | Alembic migration for `signal_jobs` table; update `SignalModel` & `SignalRepository`. | Alembic migration check + unit tests. |
| **Phase 3** | Fast Webhook Ingestion Refactor | Update `POST /api/webhooks/tradingview` in `webhooks.py` to write DB job & return HTTP 202 immediately. | Webhook latency test (< 15ms). |
| **Phase 4** | Durable Idempotency Enforcement | Unique constraints on `(strategy_id, signal_id, idempotency_key)`; atomic check in repo. | Duplicate webhook test (2x & 100x concurrent). |
| **Phase 5** | Execution Worker Pool Layer | Create `app/services/worker_pool.py` with `FOR UPDATE SKIP LOCKED` claim logic. | Worker claim & execution tests. |
| **Phase 6** | Bounded Concurrency & Locks | Add `(account_id, strategy_id)` partitioning lock pool in worker manager. | Concurrent signal safety test. |
| **Phase 7** | Refactor `BasketCoordinator` State | Remove `_active_basket` from `BasketCoordinator`; introduce isolated `BasketContext`. | Concurrent basket isolation test. |
| **Phase 8** | Account Fan-Out Parallelization | Replace sequential account fan-out loop in `order_manager.py` with `asyncio.gather()`. | Multi-account fan-out test. |
| **Phase 9** | Non-Blocking Event Loop Audit | Replace synchronous disk write in `webhooks.py` & thread `event.wait()` in CFD discover. | Event loop latency test. |
| **Phase 10** | Centralized IBKR Execution Scheduler | Implement `IBKRExecutionScheduler` with token bucket rate limiting in `ibkr_adapter.py`. | IBKR rate limiting unit tests. |
| **Phase 11** | Callback & Fill Correlation Hardening | Correlate TWS callbacks using `(account_id, basket_id, internal_order_id)`. | Callback correlation unit tests. |
| **Phase 12** | Complete Execution State Persistence | Ensure all basket/order state transitions are written to DB prior to state returns. | DB state persistence test. |
| **Phase 13** | Process Crash Recovery Manager | Implement startup recovery scanner (`RecoveryManager`) with broker snapshot reconciliation. | Restart recovery test scenarios. |
| **Phase 14** | Ordering Guarantees Enforcement | Document & enforce per-account/per-basket ordering while keeping global execution async. | Ordering verification test. |
| **Phase 15** | Backpressure & Metrics Monitoring | Expose worker queue depth, oldest job age, and processing rate via API. | Health & telemetry API tests. |
| **Phase 16** | End-to-End Log Context & Correlation | Bind correlation IDs (`request_id` -> `signal_id` -> `job_id` -> `basket_id` -> `ibkr_order_id`). | Log correlation check. |
| **Phase 17** | Frontend SSE Lifecycle Signal Sync | Update SSE stream event triggers to broadcast `QUEUED`, `PROCESSING`, `COMPLETED` statuses. | Frontend UI build + SSE test. |
| **Phase 18** | Comprehensive Automated Test Suite | Add comprehensive concurrency, crash recovery, and stress test suites. | Full pytest regression suite passes. |
| **Phase 19** | 300+ Signal Burst Load Test | Run `load_test_mft_burst.py` with 300 & 500 signals. | Load test KPI verification. |
| **Phase 20** | Production Deployment Readiness | Verify zero business logic regressions and validate paper IBKR execution end-to-end. | System ready for live deploy. |

---

## Regression Verification Protocol After Every Phase

After completing each phase:
1. Run `.venv/bin/pytest` in `backend/` to ensure no existing tests fail.
2. Run `.venv/bin/ruff check app/ tests/` to verify code quality.
3. Validate that no trading business logic (sizing, RMS thresholds, leg calculations) was modified.

---
*Step-by-Step Migration Plan.*
