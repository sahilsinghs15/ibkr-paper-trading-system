# EC2 Paper IB Gateway + Backend — Full Operations Guide

This is the onboarding map for anyone who must access, inspect, or operate the **paper trading** stack on AWS EC2.

It is written from live inspection of host `ip-172-31-5-91` (public `98.81.69.227`) as of 18 Aug 2026. Treat secrets as living in files, not in chat logs. **Never paste IBKR passwords, ngrok tokens, or the SSH private key into tickets, PRs, or Slack.**

---

## 0. Mental model (read this first)

There are **two machines** and they must stay separate:

| Machine | Role | IBKR |
|---|---|---|
| **Local Ubuntu** (`dev3-linux`) | Developer laptop / Cursor workspace | Local TWS/Gateway (including any **live** session). **Do not start, stop, log in, or reconfigure it for EC2 paper tests.** |
| **EC2** (`ubuntu@98.81.69.227`) | Paper execution host | **IB Gateway PAPER** via IBC, API **`127.0.0.1:4002`** |

The backend **does not log into IBKR**. IBC + Gateway authenticate. The backend only opens a TWS API socket to an already-logged-in Gateway.

```
TradingView  --HTTPS-->  ngrok  -->  FastAPI :8000  -->  TWS API  -->  127.0.0.1:4002  -->  IB Gateway PAPER  -->  IBKR paper account
```

**Do not** expose Gateway port 4002 on the public internet. Only `:8000` is tunneled (ngrok). SSH is `:22`.

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
├── storage/logs/                      # daily trading-YYYY-MM-DD.log
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
| App logs (workspace) | `/home/tradingapp/storage/logs/` | ops | Daily files |
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
IBKR_PORT=4002
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
| **8000** | `127.0.0.1` only | FastAPI. Reachable from internet **only via ngrok** |
| **4002** | Gateway API (paper) | Backend must use this. **Do not publish in security group.** |
| **4001** | Gateway **live** | **NEVER** point the backend here |
| **7497** | TWS paper | Local TWS paper; **not** the EC2 Gateway paper port |
| **7496** | TWS **live** | **NEVER** |
| **4040** | `127.0.0.1` | ngrok local inspector |

If `ss -lntp | grep 4002` is empty, Gateway is down. Do not send webhooks.

---

## 5. Process topology (tmux)

Two tmux sessions, created as `tradingapp`:

| Session | Pane | What runs | Typical path |
|---|---|---|---|
| `tradingapp` | **0.0** | `uv run uvicorn … :8000` | `/home/tradingapp/app/backend` |
| `tradingapp` | **0.1** | `./ngrok http 8000` | `/home/tradingapp` |
| `ibgateway` | **0.0** | IBC + Gateway (or a shell after Gateway exits) | `/home/tradingapp` or `Jts` |

Useful commands (as `tradingapp`):

```bash
tmux list-sessions
tmux list-panes -t tradingapp -F '#{pane_index} #{pane_current_command} PID=#{pane_pid} #{pane_current_path}'
tmux list-panes -t ibgateway -F '#{pane_index} #{pane_current_command} PID=#{pane_pid}'
tmux attach -t tradingapp
tmux attach -t ibgateway
tmux capture-pane -t tradingapp:0.0 -p -S -100
tmux capture-pane -t ibgateway:0.0 -p -S -100
```

Xvfb (headless display for Gateway) is **not** in tmux; it has been started as:

```bash
Xvfb :99 -screen 0 1024x768x24 >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
```

Check: `ps -ef | grep Xvfb | grep -v grep`

---

## 6. Canonical start procedure (`start.txt`)

**Source of truth:** `/home/tradingapp/start.txt`

Do not invent a different Gateway launcher. Contents:

```bash
cd /home/tradingapp/app/backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

./ngrok http 8000

Xvfb :99 -screen 0 1024x768x24 >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99

~/ibc/scripts/ibcstart.sh 1045 --gateway \
  --tws-path=/home/tradingapp/Jts \
  --tws-settings-path=/home/tradingapp/Jts \
  --ibc-path=/home/tradingapp/ibc \
  --ibc-ini=/home/tradingapp/ibc/config.ini
```

`1045` is the Gateway/TWS version IBC should start.

### 6.1 If Gateway is already healthy

```bash
ps -ef | grep -iE 'ibc|ibgateway' | grep -v grep
ss -lntp | grep -E ':4002|:4003|:7497|:4001'
```

If Java is up **and** `:4002` is listening **and** IBC recently logged `Login has completed`, **do not restart Gateway**.

### 6.2 If Gateway is down

1. Confirm Xvfb `:99` is running (start it only if missing).
2. In `ibgateway:0.0` (as `tradingapp`):

```bash
export DISPLAY=:99
~/ibc/scripts/ibcstart.sh 1045 --gateway \
  --tws-path=/home/tradingapp/Jts \
  --tws-settings-path=/home/tradingapp/Jts \
  --ibc-path=/home/tradingapp/ibc \
  --ibc-ini=/home/tradingapp/ibc/config.ini
```

3. Wait for IBC: `Setting Trading mode = paper`, `Paper Log In`, `Login has completed`.
4. Confirm `ss -lntp | grep 4002`.
5. **Then** restart the backend (section 7). Never start uvicorn before Gateway is ready if you just brought Gateway up.

Paper sessions can auto-exit (“Exit Session Setting (Simulated Trading)”). That is normal overnight. Restart Gateway with the same command; do not create a second login while one is healthy.

---

## 7. Backend start / restart

Only after Gateway is on **4002**:

```bash
tmux send-keys -t tradingapp:0.0 C-c
# wait until :8000 is gone
tmux send-keys -t tradingapp:0.0 \
  'cd /home/tradingapp/app/backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000' \
  Enter
tmux capture-pane -t tradingapp:0.0 -p -S -80
```

Ready means **all** of:

- `Attempting TWS connection to 127.0.0.1:4002`
- `TWS nextValidId received` / `Handshake complete`
- `Active execution pipeline: … TWS 127.0.0.1:4002`
- `Paper-trading execution application is ready`
- `curl -s http://127.0.0.1:8000/health` → `{"status":"ok"}`

Uvicorn “Started server process” alone is **not** ready.

OpenAPI (on the box): `http://127.0.0.1:8000/docs`  
Webhook: `POST /api/webhooks/tradingview`

---

## 8. What the EC2 backend actually does

EC2 git (Aug 2026): `main` @ `86485a6`.

This **is not** the same tree as a developer laptop that has DB-1/DB-2/DB-3 work. On EC2 today:

- Allocations: **YAML** `config/paper_allocations.yaml` (`$100000 × 0.25 = $25000` committed)
- Open trades: **in-memory** `PositionBook` — **lost on uvicorn restart**
- Adapter maps payload `STK` → IBKR **CFD** at `placeOrder`
- `placeOrder` currently sets OMS status **`SUBMITTED` immediately** (Stage 1 acknowledgement fix lives on the laptop repo, **not** deployed here unless someone ships it)
- RMS open-position count is in-memory; a restart **resets** it (last incident: 10/10 `OPEN_POSITION_LIMIT` rejected live TradingView OPENs)
- `app/db` on that commit is incomplete — do not import it on EC2 until that phase is deployed

Local laptop (this git workspace) may have PostgreSQL on Docker `:5433`, Alembic, strategy registry, and PENDING-until-broker-ack. **Do not assume EC2 has those until you compare git SHAs.**

---

## 9. Paper allocation (EC2)

File: `/home/tradingapp/app/backend/config/paper_allocations.yaml`

Observed:

- account `paper1`, `total_margin: 100000`, enabled
- strategy `model_blue`, `max_open_positions: 10`
- `alloc_pct: 0.25` → **committed = 25000**
- per-symbol money limits for SIL / GDX in YAML (other symbols may use RMS defaults)

Do **not** raise `max_open_positions` or invent capital just to force a test through.

---

## 10. ngrok / TradingView

- Binary: `/home/tradingapp/ngrok`
- Config: `/home/tradingapp/.ngrok2/ngrok.yml`
- Typical: pane `tradingapp:0.1` running `./ngrok http 8000`
- Local ngrok UI on the instance: `127.0.0.1:4040`

TradingView must POST to the **ngrok HTTPS URL** + `/api/webhooks/tradingview`. If ngrok is down, alerts never hit uvicorn.

Do not point TradingView at Gateway `:4002`.

---

## 11. Health checks before any paper order

Copy this checklist. If any box fails, **STOP**. Do not “fix” by switching to 4001 or touching local live TWS.

- [ ] SSH as `ubuntu`, ops as `tradingapp`
- [ ] `TradingMode=paper` in IBC; `tradingMode=p` in `jts.ini`
- [ ] IBC process + Java Gateway process
- [ ] `:4002` listening; **not** `:4001`
- [ ] IBC log: login completed / paper
- [ ] Backend `.env` `IBKR_PORT=4002`
- [ ] uvicorn handshake to `127.0.0.1:4002`, no **Error 321** (read-only API)
- [ ] YAML allocation present
- [ ] Unique `trade_id` (not already in PositionBook / captures)
- [ ] You are **not** logged into the same IBKR user on another Gateway if you cannot afford `ExistingSessionDetectedAction=primary` kicking that session

Then one webhook through the normal path: webhook → mapper → OrderManager → sizer → RMS → OMS → adapter → Gateway. **Never** `placeOrder` from a shell script.

---

## 12. Hard never-do list

- Live ports **4001 / 7496**
- Change backend to live
- Open 4002 in the AWS security group
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
| IBKR | Separate; may be live — **hands off** for paper EC2 tests | Paper Gateway **4002** |
| Backend `.env` | Often `IBKR_PORT=7497`, may have `DATABASE_URL` to Docker `:5433` | `IBKR_PORT=4002`, YAML alloc, no Postgres |
| Code | May be ahead (DB, strategy registry, PENDING ack) | Deployed SHA may lag |
| Docker Postgres | `ibkr-postgres` `:5433` (`root` / see `docker-compose.yml`) | Not used |

Local `backend/.env` `BROKER_MODE=mock` is ignored by current Settings (`extra=ignore`) unless the running code reads it. **EC2** sets `BROKER_MODE=ibkr` in `.env` and connects for real.

---

## 14. Day-one script for a new developer

1. Get the PEM; `chmod 400` it.
2. `ssh -i … ubuntu@98.81.69.227` then `sudo su tradingapp`.
3. `cat ~/start.txt` and `cat ~/app/AGENTS.md`.
4. `tmux list-sessions` and capture both panes.
5. `ss -lntp | grep -E '8000|4002|4001'`.
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
