# 14-TESTING-DIAGNOSTICS.md — Testing & Diagnostic Protocol

---

## 1. THE DIAGNOSTIC LADDER

Every agent must run the diagnostic ladder before concluding work:

```
[Level 1: Linting]
ruff check app/ tests/ scripts/
      │
      ▼
[Level 2: Backend Type Checking]
mypy app/
      │
      ▼
[Level 3: Frontend Type Checking]
cd frontend && npx tsc --noEmit
      │
      ▼
[Level 4: Targeted Unit Tests]
pytest tests/path/to/relevant_test.py
      │
      ▼
[Level 5: Integration Regression Suite]
pytest
      │
      ▼
[Level 6: Logical & Safety Verification]
Inspect diff, verify DB constraints, prove lock order
```

---

## 2. RUNNING TESTS CORRECTLY

### Database Isolation & Truncate Deadlocks
- Tests run against `ibkr_trading_test` (default on port 5433).
- **CRITICAL WARNING**: Running multiple `pytest` processes concurrently will DEADLOCK on `TRUNCATE TABLE ... CASCADE` relation locks!
- Always ensure no dangling background `pytest` processes are running before launching a test suite:
  ```bash
  ps aux | grep pytest
  ```

### Pytest Environment
- Automatically sets `TRADINGAPP_TESTING="1"` and `WEBHOOK_AUTH_ENABLED="false"` via `tests/conftest.py`.
- Runs migrations up to head on the test database at session startup.

---

## 3. STRICT DIAGNOSTIC REPORTING

An agent final report must classify every diagnostic into one of five states:
- **PASS**: Executed with exit code 0.
- **FAIL**: Executed with non-zero exit code or error output.
- **BLOCKED**: Execution prevented by external dependency (e.g. relation locks, missing credentials).
- **NOT RUN**: Deliberately omitted with documented justification.
- **NOT APPLICABLE**: Irrelevant to the change (e.g. frontend build when changing backend-only script).

Reporting a status of **COMPLETE** requires that all applicable diagnostics are verified with zero unaddressed regressions.
