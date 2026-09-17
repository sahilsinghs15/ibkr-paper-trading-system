# 17-GIT-DEPLOYMENT.md — Git Governance & Production Deployment

---

## 1. GIT SAFETY RULES

- **Strictly Prohibited**:
  - `git reset --hard` (destroys uncommitted code).
  - `git clean -fd` (removes untracked work).
  - Force pushing to `main` or release branches.
  - Unrelated formatting sweeps across untouched files.
- **Diff Hygiene**:
  - Always run `git diff --stat` before committing.
  - Commit messages must follow Conventional Commits (e.g. `feat(manual): ...`, `fix(reconcile): ...`).

---

## 2. PRODUCTION SYSTEMD UNITS

Production execution runs as systemd services under `/etc/systemd/system/` (mirrored in [`deploy/`](file:///home/dev3/Documents/ibkr-paper-trading-system/deploy)):
1. `ibgateway.service`: Runs the headless IB Gateway daemon.
2. `trading-backend.service`: Runs `app.main:app` on port 8001.
3. `webhook-ingest.service`: Runs `app.webhook_ingest:app` on port 8000.
4. **Note**: `process-manager.service` is deprecated and must remain disabled.

---

## 3. SAFE DEPLOYMENT WORKFLOW

1. Pull latest verified commits:
   ```bash
   git pull origin main
   ```
2. Sync dependencies:
   ```bash
   uv sync
   ```
3. Run database migrations:
   ```bash
   .venv/bin/alembic upgrade head
   ```
4. Restart services sequentially:
   ```bash
   sudo systemctl restart trading-backend
   sudo systemctl restart webhook-ingest
   ```
5. Verify health:
   ```bash
   curl http://127.0.0.1:8001/health
   curl http://127.0.0.1:8000/health
   ```
