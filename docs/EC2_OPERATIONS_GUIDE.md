# EC2 Paper IB Gateway + Backend — Full Operations Guide

This is the onboarding map for anyone who must access, inspect, or operate the **paper trading** stack on AWS EC2.

It is written from live inspection of host `ip-172-31-5-91` (public `98.81.69.227`) as of 18 Aug 2026. Treat secrets as living in files, not in chat logs. **Never paste IBKR passwords, ngrok tokens, or the SSH private key into tickets, PRs, or Slack.**

**Accuracy vs current application code (not the host snapshot):**

| Claim in this file (dated 18 Aug 2026) | Current code (`app/core/config.py` and routers) |
|----------------------------------------|--------------------------------------------------|
| `BROKER_MODE=ibkr` on EC2 `.env` | **Ignored.** `Settings` has `extra="ignore"` and no `BROKER_MODE` field. |
| “There is no `DATABASE_URL` on EC2 today. Capital comes from YAML.” | **STALE vs product.** Trading app requires Postgres (`DATABASE_URL`); allocations live in `accounts` / `allocations` tables. YAML is not the runtime router. |
| One Gateway on `127.0.0.1:4001` | **ACCURATE as-is topology:** the app still opens **one** `TWSClient`. N Gateways are target-only — [`backend-multi-gateway.md`](backend-multi-gateway.md). |

Keep using this file for SSH / IBC / tmux paths on that host. For application behavior, prefer [`backend-config.md`](backend-config.md) and [`backend-execution.md`](backend-execution.md).

---

## 0. Mental model (read this first)

There are **two machines** and they must stay separate:

| Machine | Role | IBKR |
|---|---|---|
| **Local Ubuntu** (`dev3-linux`) | Developer laptop / Cursor workspace | Local TWS/Gateway (including any **live** session). **Do not start, stop, log in, or reconfigure it for EC2 paper tests.** |
| **EC2** (`ubuntu@98.81.69.227`) | Paper execution host | **IB Gateway PAPER** via IBC, API **`127.0.0.1:4001`** |

The backend **does not log into IBKR**. IBC + Gateway authenticate. The backend only opens a TWS API socket to an already-logged-in Gateway.

```
TradingView  --HTTPS-->  ngrok  -->  Webhook ingest :8000  -->  Postgres (signal_jobs)
                                                              -->  Trading app :8001  -->  TWS API  -->  127.0.0.1:4001  -->  IB Gateway PAPER
```

**Do not** expose Gateway port 4001 on the public internet. Only webhook ingest `:8000` is tunneled (ngrok). SSH is `:22`.

---

## 1. How to get on the box

### 1.1 SSH (always as `ubuntu` first)

```bash
ssh -i /home/dev3/Downloads/trading-system-dev.pem ubuntu@98.81.69.227
```

| Item | Value |
|---|---|
| Host | `98.81.69.227` |
| SSH user | `ubuntu` |
| Key file (local) | `/home/dev3/Downloads/trading-system-dev.pem` |
| Key perms | must be `400` (`-r--------`) |
| Internal hostname | `ip-172-31-5-91` |
| Private IP | `172.31.5.91` |

If the key is missing, you cannot invent a new one. Ask the owner for the existing PEM (do not generate a replacement Gateway login).

### 1.2 Switch to the application user

```bash
sudo su tradingapp
# or
sudo -u tradingapp -H bash
```

| Item | Value |
|---|---|
| App OS user | `tradingapp` (uid 1001) |
| Home | `/home/tradingapp` |
| All IBC / Gateway / uvicorn / ngrok work | **as `tradingapp`** |

Do **not** run Gateway, IBC, or uvicorn as `ubuntu`.

---

## 2. Directory map (`/home/tradingapp`)

```
/home/tradingapp/
├── AGENTS.md                          # pointer into app/AGENTS.md
├── Execution_System_Architecture.md   # TARGET design (Postgres/Redis) — NOT current EC2 runtime
├── start.txt                          # canonical start commands (copy from here; do not invent)
├── app/                               # git product repo
│   ├── AGENTS.md
│   ├── Readme.md
│   ├── docs/                          # backend-execution, safety, API, config
│   ├── backend/                       # FastAPI app (this is the product)
│   └── frontend/                      # Vite scaffold only
├── ibc/                               # IBC install + config.ini (SECRETS)
├── Jts/                               # IB Gateway settings + installed Gateway bits
├── storage/logs/                      # daily {YYYY-MM-DD}/*.log directories
├── ngrok                              # ngrok binary
├── .ngrok2/ngrok.yml                  # ngrok authtoken (SECRET)
└── ibgateway-stable-standalone-linux-x64.sh   # Gateway installer (already used)
```

There is **no** `/home/tradingapp/ibgateway/` directory. Gateway lives under **`/home/tradingapp/Jts/ibgateway/`**. IBC lives under **`/home/tradingapp/ibc/`**.

---

## 3. Credential and config files (paths only)

**Do not commit these. Do not dump passwords into git.**

| Secret / config | Path | Who uses it | Notes |
|---|---|---|---|
| SSH private key | **Local:** `/home/dev3/Downloads/trading-system-dev.pem` | humans | Never copy onto EC2. Never commit. |
| IBKR paper login | `/home/tradingapp/ibc/config.ini` (`IbLoginId`, `IbPassword`) | IBC | Mode `600`. Paper login id is in this file. **Do not print `IbPassword`.** |
| IBC behaviour | same `config.ini` | IBC | `TradingMode`, `ExistingSessionDetectedAction`, `AcceptIncomingConnectionAction` |
| Gateway UI/settings | `/home/tradingapp/Jts/jts.ini` | Gateway | `tradingMode=p` (paper), `ApiOnly=true`, `TrustedIPs=127.0.0.1` |
| Encrypted Gateway prefs | `/home/tradingapp/Jts/laelfehkfnnjfkachiocaledpplomiocfeeccfca/ibg.xml` | Gateway | Binary/encrypted. Do not hand-edit. |
| Alternate settings dir IBC may use | `/home/tradingapp/Jts/ibgateway/1045/` | IBC/Gateway | IBC log may say “TWS Settings directory is …/ibgateway/1045” |
| Backend env | `/home/tradingapp/app/backend/.env` | uvicorn | `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`. **No IBKR password.** |
| Env template | `/home/tradingapp/app/backend/.env.example` | humans | Safe to read |
| Paper capital YAML | `/home/tradingapp/app/backend/config/paper_allocations.yaml` | backend | `total_margin × alloc_pct` → committed notional |
| ngrok token | `/home/tradingapp/.ngrok2/ngrok.yml` | ngrok | **SECRET** |
| Webhook captures | `/home/tradingapp/app/backend/data/tradingview_webhooks/` | backend | gitignored JSON dumps |
| App logs (workspace) | `/home/tradingapp/storage/logs/{YYYY-MM-DD}/` | ops | Daily directories |
| IBC user guide | `/home/tradingapp/ibc/userguide.pdf` | humans | Official IBC docs |

### 3.1 IBC keys that matter (names, not values)

From `config.ini` (inspect with `grep`; redact password):

- `TradingMode` — must be `paper`
- `IbLoginId` / `IbPassword` — paper credentials
- `ExistingSessionDetectedAction=primary` — if the same IBKR user is logged in elsewhere, this session **takes over**
- `AcceptIncomingConnectionAction=accept` — API clients
- `MinimizeMainWindow`
- `IbDir`
- Commented examples exist: `#ReadOnlyApi=no`, `#OverrideTwsApiPort=4002` — **do not uncomment/change unless explicitly instructed**

### 3.2 Backend `.env` keys on EC2 (as deployed)

```
BROKER_MODE=ibkr
IBKR_HOST=127.0.0.1
IBKR_PORT=4001
IBKR_CLIENT_ID=1
IBKR_CONNECTION_TIMEOUT=10
IBKR_MARKET_DATA_* ...
```

There is **no** `DATABASE_URL` on EC2 today. Capital comes from YAML.

### 3.3 Paper account identity (non-secret)

Observed after TWS handshake:

- Paper login flow: IBC clicks **Paper Log In**
- TWS `managedAccounts`: **`DUR919062`**
- YAML still lists placeholder `ibkr_account: DU000000` — cosmetic; Gateway account id is `DUR919062`

---

## 4. Ports (memorize these)

| Port | Binding | Meaning |
|---|---|---|
| **22** | public | SSH |
| **8000** | `127.0.0.1` only | Webhook ingest (`app.webhook_ingest:app`). Reachable from internet **only via ngrok** |
| **8001** | `127.0.0.1` only | Trading / execution (`app.main:app`). Local only |
| **4001** | Gateway API | Backend must use this (`Settings.ibkr_port` default). **Do not publish in security group.** |
| **4002** | Gateway paper (IB default) | Not used on this host; systemd `ibgateway.service` binds **4001** |
| **7497** | TWS paper | Local TWS paper; **not** the EC2 Gateway paper port |
| **7496** | TWS **live** | **NEVER** |
| **4040** | `127.0.0.1` | ngrok local inspector |

If `ss -lntp | grep 4001` is empty, Gateway is down. Do not send webhooks.

---

## 5. Process topology (removed)

tmux is **not** the production run path. systemd units own the stack:
`trading-backend.service`, `webhook-ingest.service`, `ibgateway.service`.
Do not enable `process-manager.service`. Do not start a second uvicorn or Gateway.

## 6. Canonical start procedure (removed)

Do not follow `start.txt` or launch a second Gateway/uvicorn from this file.
Use systemd: `systemctl enable --now trading-backend webhook-ingest ibgateway`.

## 7. Backend start / restart

```bash
sudo systemctl restart trading-backend.service
sudo systemctl restart webhook-ingest.service
curl -s http://127.0.0.1:8001/health
curl -s http://127.0.0.1:8000/health
```

Ready means handshake to `127.0.0.1:4001`. Uvicorn “Started server process” alone is not ready.

## 10. ngrok / TradingView

- Binary: `/home/tradingapp/ngrok`
- Config: `/home/tradingapp/.ngrok2/ngrok.yml`
- Local ngrok UI on the instance: `127.0.0.1:4040`

TradingView must POST to the **ngrok HTTPS URL** + `/api/webhooks/tradingview` with `X-Webhook-Secret`. If ngrok is down, alerts never hit uvicorn.

Do not point TradingView at the Gateway API port.

---

## 12. Hard never-do list

- Open Gateway API in the AWS security group
- Start a second uvicorn or Gateway while one is healthy
- Edit IBC/Gateway config “to make a test pass”
- Delete `Jts/` or `ibc/config.ini`
- `docker compose down -v` on any DB you do not own (EC2 currently has **no** docker Postgres)
- Second Gateway login while one is healthy
- Touch **local Ubuntu** TWS/IBKR for EC2 paper work
- Multiple retry orders after an ambiguous broker callback
- Commit `.env`, `config.ini`, `ngrok.yml`, or the PEM

---

## 13. Local developer laptop vs EC2 (do not mix)

| | Local workspace | EC2 `tradingapp` |
|---|---|---|
| SSH | n/a | PEM + ubuntu |
| IBKR | Separate; may be live — **hands off** for paper EC2 tests | Paper Gateway **4001** |
| Backend `.env` | Often `IBKR_PORT=7497`, may have `DATABASE_URL` to Docker `:5433` | `IBKR_PORT=4001`, YAML alloc, no Postgres |
| Code | May be ahead (DB, strategy registry, PENDING ack) | Deployed SHA may lag |
| Docker Postgres | `ibkr-postgres` `:5433` (`root` / see `docker-compose.yml`) | Not used |

Local `backend/.env` `BROKER_MODE=mock` is ignored by current Settings (`extra=ignore`) unless the running code reads it. **EC2** sets `BROKER_MODE=ibkr` in `.env` and connects for real.

---

## 14. Day-one script for a new developer

1. Get the PEM; `chmod 400` it.
2. `ssh -i … ubuntu@98.81.69.227` then `sudo su tradingapp`.
3. `cat ~/start.txt` and `cat ~/app/AGENTS.md`.
4. `tmux list-sessions` and capture both panes.
5. `ss -lntp | grep -E '8000|8001|4001'`.
6. Confirm paper + handshake in backend pane.
7. Read `~/app/docs/backend-execution.md` and `~/app/docs/safety.md` **before** any webhook.
8. Compare `git -C ~/app log -1` with the laptop repo before assuming Stage-N behaviour.

---

## 15. Related docs on the server

| File | Use |
|---|---|
| `/home/tradingapp/app/AGENTS.md` | Invariants for the **deployed** app |
| `/home/tradingapp/app/docs/backend-execution.md` | Debug OPEN/CLOSE |
| `/home/tradingapp/app/docs/safety.md` | Paper vs live |
| `/home/tradingapp/app/docs/backend-config.md` | Env + YAML |
| `/home/tradingapp/app/docs/backend-api.md` | HTTP |
| `/home/tradingapp/Execution_System_Architecture.md` | Future architecture — not current |
| `/home/tradingapp/ibc/userguide.pdf` | IBC |

This operations guide lives in the laptop repo at `docs/EC2_OPERATIONS_GUIDE.md`. Keep it in sync when host, ports, or tmux layout change.
