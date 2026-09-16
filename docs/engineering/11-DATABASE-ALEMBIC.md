# 11-DATABASE-ALEMBIC.md — Persistence, Transactions & Alembic

---

## 1. POSTGRESQL & ASYNC ENGINE

- **Engine Driver**: `asyncpg` via SQLAlchemy 2.0 `create_async_engine`.
- **Session Factory**: `AsyncSessionLocal` (`expire_on_commit=False`, `autoflush=False`).
- **Connection Isolation**: Configured via `DATABASE_URL` in `.env`.

---

## 2. TRANSACTION BOUNDARIES & LOCKING

### Row-Level Locking
- When mutating positions or recording executions, code **MUST** acquire row-level locks to prevent concurrent race conditions:
  ```python
  stmt = (
      select(ManualPositionModel)
      .where(ManualPositionModel.account_id == account_id, ManualPositionModel.trade_id == trade_id)
      .with_for_update()
  )
  ```

### Two-Phase Commits for Broker Calls
- **Rule**: Never hold a database transaction open while awaiting an external broker socket network round-trip.
- **Manual Order Submission Pattern**:
  1. Insert order record with status `PENDING_SUBMIT`.
  2. `await session.commit()` (persists to Postgres).
  3. Call `TWSClient.placeOrder()`.
  4. Update order record with status `SUBMITTED`, `broker_order_id`, and `submitted_at`.
  5. `await session.commit()`.

---

## 3. ALEMBIC MIGRATION WORKFLOW

Alembic ([`backend/alembic/`](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/alembic)) is the sole schema authority for production.

### Safe Migration Lifecycle
1. **Generating Migrations**:
   ```bash
   cd backend
   .venv/bin/alembic revision --autogenerate -m "descriptive_name"
   ```
2. **Review Generated Code**: Inspect the generated Python file under `alembic/versions/`. Ensure foreign keys, unique constraints, and nullable attributes match domain requirements.
3. **Upgrade Command**:
   ```bash
   .venv/bin/alembic upgrade head
   ```

### Prohibited Database Practices
- **NEVER run `Base.metadata.create_all()`** in application production entrypoints.
- **NEVER perform destructive table wipes** or drops on live or paper trading tables.
- **NEVER add non-nullable columns without defaults** to tables with existing rows.
