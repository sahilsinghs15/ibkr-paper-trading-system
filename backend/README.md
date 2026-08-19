# Backend

FastAPI IBKR paper execution engine for this repo.

## Agent docs

Prefer [`../AGENTS.md`](../AGENTS.md) and [`../docs/`](../docs/) over older guides in this folder.

## Run

```bash
cd /home/tradingapp/app/backend
uv sync --extra dev   # or use existing .venv
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Test / lint

```bash
.venv/bin/pytest
.venv/bin/ruff check app/ tests/ scripts/
```

## Demo positions UI (separate process)

```bash
.venv/bin/python -m demo_streaming
# http://127.0.0.1:8010/

# Listen on all interfaces (server public IP; open AWS SG TCP 8010):
DEMO_STREAM_HOST=0.0.0.0 .venv/bin/python -m demo_streaming
```

Serves React `frontend/dist` when built (`npm run build` in `app/frontend`, Node ≥ 20); otherwise HTML fallback. Does not place orders. Keep ngrok on `:8000` only.

## Note

`main.py` in this directory is a hello stub. The real app is `app.main:app`.
