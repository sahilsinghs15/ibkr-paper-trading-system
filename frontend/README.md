# Frontend

Vite + React 19 + TypeScript **live PnL dashboard**. Reads the demo stream (`/demo/positions` + SSE `/demo/stream`). Does not place orders.

## Docs

Agent-oriented frontend facts: [`../docs/frontend.md`](../docs/frontend.md).

## Requirements

- **Node.js ≥ 20** (Vite 8). System Node 18 is too old for `npm run build`.

## Local dev (proxy to demo)

```bash
# Terminal A — Redis + Postgres required
cd /home/tradingapp/app/backend
.venv/bin/python -m demo_streaming

# Terminal B
cd /home/tradingapp/app/frontend
npm install
npm run dev
# http://127.0.0.1:5173/  (proxies /demo → :8010)
```

## Production build (served by `:8010`)

```bash
cd /home/tradingapp/app/frontend
npm run build
# writes dist/; demo_streaming GET / serves it
```

Remote via server IP:

```bash
cd /home/tradingapp/app/backend
DEMO_STREAM_HOST=0.0.0.0 .venv/bin/python -m demo_streaming
# AWS SG: inbound TCP 8010 from your IP
# http://PUBLIC_IP:8010/
```

Keep `./ngrok http 8000` for TradingView only. Do not tunnel the dashboard through the webhook URL.

## HTML fallback

`positions-demo.html` (byte-identical to `backend/demo_streaming/static/index.html`) is used when `frontend/dist` is missing.

## Declared deps

| Package | Role |
|---------|------|
| `axios`, `zustand` | Snapshot + live state |
| `@tanstack/react-query`, `react-router-dom`, `lightweight-charts`, `tailwindcss` | Declared; unused in this pass |
