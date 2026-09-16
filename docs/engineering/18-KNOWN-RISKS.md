# 18-KNOWN-RISKS.md — Known Architectural Risks & Edge Cases

---

## 1. KNOWN SYSTEM RISKS

### Risk 1: Test Database Deadlocks via `TRUNCATE CASCADE`
- **Location**: `backend/tests/conftest.py`
- **Description**: Running concurrent pytest sessions against `ibkr_trading_test` causes transactions to deadlock on relation locks when one test runner issues `TRUNCATE TABLE ... CASCADE`.
- **Precaution**: Ensure no background pytest instances are running before launching a test suite.

### Risk 2: Single IBKR Socket Pacing Contention
- **Location**: `backend/app/broker/ibkr/gateway_rate_limiter.py`
- **Description**: All accounts share a single Gateway rate limiter (30 msg/sec). If one account floods orders or cancellations, other accounts experience `GatewayPacingTimeout`.
- **Precaution**: Preserve priority buckets (P0 flatten reserve, P1 executions).

### Risk 3: Stale Market Data Marks for Inactive CFDs
- **Location**: `backend/app/services/pnl.py`
- **Description**: CFDs on low-liquidity instruments may not emit trade ticks outside European hours.
- **Precaution**: The system uses underlying STK contracts (`market_data_con_id`) for marks and falls back to mid-price `(bid+ask)/2` or previous close. Never invent prices or use entry price.

### Risk 4: Node.js Runtime Version Mismatch
- **Location**: `frontend/node_modules/`
- **Description**: The Vite/Rolldown build toolchain requires Node.js $\ge 20.12.0$ due to `styleText` from `node:util`. Building on Node 18 fails during bundle creation, although `tsc` type checking succeeds.
- **Precaution**: Ensure the deployment host runs Node $\ge 20$.

### Risk 5: Out-of-Order IBKR Callbacks
- **Location**: `backend/app/services/manual_callbacks.py`
- **Description**: `commissionReport` can arrive before `execDetails`.
- **Precaution**: Preserve the in-memory pending commission buffer (`_pending_commissions`).
