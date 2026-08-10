# Postman API Testing Guide — IBKR Paper Trading System

**Backend Version:** 0.1.0  
**OpenAPI Specification:** 3.1.0  
**Default Base URL:** `http://127.0.0.1:8000`  
**Configuration File:** Loaded via Environment Variables / `.env`

---

## 1. System Architecture & Broker Modes

The application supports two execution modes controlled by the `BROKER_MODE` configuration setting.

### Mock Mode (`BROKER_MODE=mock`) — Default

```
Client / Postman
   ↓
FastAPI Routes
   ↓
BaseBroker (Abstraction)
   ↓
MockBroker (In-Memory State)
```

- In-memory order tracking and position management.
- Static default margin values (`equity=1000000`, `available_funds=800000`, `buying_power=1600000`).
- Orders remain in `SUBMITTED` state unless programmatically filled.
- TWS connection is **not required**.

### IBKR Mode (`BROKER_MODE=ibkr`) — Production / Demo

```
Client / Postman
   ↓
FastAPI Routes
   ↓
BaseBroker (Abstraction)
   ↓
IBKRBroker
   ↓
TWSClient (Socket Session)
   ↓
IBKR TWS / Gateway (Paper Trading Port 7497)
```

- Socket connection to TWS managed via application lifespan.
- Real-time account summary and position callbacks from TWS.
- Orders transmitted to TWS paper trading socket; live order status updates mapped from TWS callbacks (`openOrder`, `orderStatus`).
- Streaming market data via `IBKRMarketDataAdapter` → background task → strategy pipeline.

---

## 2. Server URL & Configuration Details

### Server Network Address

| Property | Value | Configuration / Source |
|:---|:---|:---|
| **Host** | `127.0.0.1` | Default Uvicorn host parameter (`--host 127.0.0.1`) |
| **Port** | `8000` | Default Uvicorn port parameter (`--port 8000`) |
| **Protocol** | `http` | Standard HTTP |
| **Base URL** | `http://127.0.0.1:8000` | Full base URL for all endpoints |

> [!NOTE]
> The HTTP server port (`8000`) is distinct from the IBKR TWS API socket port (`7497`).

### Configuration Variables (`app/core/config.py`)

| Environment Variable | Settings Field | Default Value | Description |
|:---|:---|:---|:---|
| `BROKER_MODE` | `broker_mode` | `"mock"` | Active broker selection (`"mock"` or `"ibkr"`) |
| `IBKR_HOST` | `ibkr_host` | `"127.0.0.1"` | Hostname or IP of IBKR TWS / Gateway |
| `IBKR_PORT` | `ibkr_port` | `7497` | TWS socket port (`7497` for paper trading, `7496` for live) |
| `IBKR_CLIENT_ID` | `ibkr_client_id` | `1` | Unique API client ID |
| `IBKR_CONNECTION_TIMEOUT` | `ibkr_connection_timeout` | `10` | Timeout in seconds for TWS connection handshake |
| `IBKR_MARKET_DATA_TYPE` | `ibkr_market_data_type` | `3` | `1`=Live, `2`=Frozen, `3`=Delayed, `4`=Delayed Frozen |
| `IBKR_MARKET_DATA_SYMBOL` | `ibkr_market_data_symbol` | `"AAPL"` | Contract symbol for TWS market data streaming |
| `IBKR_MARKET_DATA_SEC_TYPE` | `ibkr_market_data_sec_type` | `"STK"` | Security type (`STK`, `OPT`, `FUT`, `IND`, etc.) |
| `IBKR_MARKET_DATA_EXCHANGE` | `ibkr_market_data_exchange` | `"SMART"` | Routing exchange |
| `IBKR_MARKET_DATA_CURRENCY` | `ibkr_market_data_currency` | `"USD"` | Currency denomination |
| `TRADING_SYMBOL` | `trading_symbol` | `"RELIANCE"` | Symbol used by strategy order manager |

---

## 3. OpenAPI Schema & Route Verification

The application exposes built-in API documentation:
- **Swagger UI**: `http://127.0.0.1:8000/docs`
- **ReDoc**: `http://127.0.0.1:8000/redoc`
- **OpenAPI 3.1 JSON Schema**: `http://127.0.0.1:8000/openapi.json`

### Route Verification Report

| Source Code Route | OpenAPI Path | Discrepancy Status |
|:---|:---|:---|
| `GET /health` | `/health` | ✅ 100% Match |
| `POST /api/v1/market-data` | `/api/v1/market-data` | ✅ 100% Match |
| `POST /api/v1/market-data/subscribe` | `/api/v1/market-data/subscribe` | ✅ 100% Match |
| `DELETE /api/v1/market-data/subscribe` | `/api/v1/market-data/subscribe` | ✅ 100% Match |
| `GET /api/v1/orders` | `/api/v1/orders` | ✅ 100% Match |
| `POST /api/v1/orders` | `/api/v1/orders` | ✅ 100% Match |
| `PUT /api/v1/orders/{order_id}` | `/api/v1/orders/{order_id}` | ✅ 100% Match |
| `DELETE /api/v1/orders/{order_id}` | `/api/v1/orders/{order_id}` | ✅ 100% Match |
| `GET /api/v1/positions` | `/api/v1/positions` | ✅ 100% Match |
| `GET /api/v1/margin` | `/api/v1/margin` | ✅ 100% Match |
| `GET /api/v1/broker/status` | `/api/v1/broker/status` | ✅ 100% Match |

> [!NOTE]
> **No discrepancies found.** All 11 registered FastAPI application routes match the OpenAPI specification exactly.

---

## 4. Complete Endpoint Inventory & Postman Testing Guide

---

### Endpoint 1: Health Check

#### Method
`GET`

#### Full URL
`http://127.0.0.1:8000/health`

#### Purpose
Verify that the backend application process is alive and responsive.

#### Headers
None

#### Path Parameters
None

#### Query Parameters
None

#### Request Body
No request body.

#### Possible Response Status Codes
- `200 OK`: Application process is running cleanly.

#### Response Body Schema (`200 OK`)
```json
{
  "status": "ok"
}
```

#### Actual Tested Response
```json
{
  "status": "ok"
}
```

#### Execution Metadata
- **Broker**: None (Independent health check)
- **TWS Required**: No
- **Safe to Execute Manually**: ✅ Yes

---

### Endpoint 2: Get Broker Connection Status

#### Method
`GET`

#### Full URL
`http://127.0.0.1:8000/api/v1/broker/status`

#### Purpose
Retrieve active broker mode, concrete broker class name, and TWS connection status.

#### Headers
None

#### Path Parameters
None

#### Query Parameters
None

#### Request Body
No request body.

#### Possible Response Status Codes
- `200 OK`: Status retrieved successfully.

#### Response Body Schema (`200 OK` — `BrokerStatusResponse`)
```json
{
  "broker_mode": "mock | ibkr",
  "connected": true,
  "broker_type": "MockBroker | IBKRBroker"
}
```

#### Actual Tested Response (IBKR Mode with TWS)
```json
{
  "broker_mode": "ibkr",
  "connected": true,
  "broker_type": "IBKRBroker"
}
```

#### Actual Tested Response (Mock Mode)
```json
{
  "broker_mode": "mock",
  "connected": true,
  "broker_type": "MockBroker"
}
```

#### Execution Metadata
- **Broker**: Reads application state (`app.state.broker`, `app.state.broker_mode`)
- **TWS Required**: No (returns `connected: false` if TWS is disconnected)
- **Safe to Execute Manually**: ✅ Yes

---

### Endpoint 3: Get Account Margin Info

#### Method
`GET`

#### Full URL
`http://127.0.0.1:8000/api/v1/margin`

#### Purpose
Retrieve total equity, available funds, and buying power from the active broker.

#### Headers
None

#### Path Parameters
None

#### Query Parameters
None

#### Request Body
No request body.

#### Possible Response Status Codes
- `200 OK`: Account margin retrieved successfully.
- `503 Service Unavailable`: TWS connection lost or request timed out (IBKR mode).

#### Response Body Schema (`200 OK` — `MarginSchema`)
```json
{
  "equity": "decimal-string",
  "available_funds": "decimal-string",
  "buying_power": "decimal-string"
}
```

#### Actual Tested Response (IBKR Mode — Live TWS Paper Account `DUR793682`)
```json
{
  "equity": "250000.00",
  "available_funds": "250000.00",
  "buying_power": "250000.00"
}
```

#### Actual Tested Response (Mock Mode)
```json
{
  "equity": "1000000",
  "available_funds": "800000",
  "buying_power": "1600000"
}
```

#### Execution Metadata
- **Broker**: `IBKRBroker.get_margin()` in IBKR mode; `MockBroker.get_margin()` in Mock mode
- **TWS Required**: Yes in IBKR mode (requests `reqAccountSummary` from TWS socket)
- **Safe to Execute Manually**: ✅ Yes (Read-only)

---

### Endpoint 4: Get Current Positions

#### Method
`GET`

#### Full URL
`http://127.0.0.1:8000/api/v1/positions`

#### Purpose
Retrieve all open (non-flat) portfolio positions from the active broker.

#### Headers
None

#### Path Parameters
None

#### Query Parameters
None

#### Request Body
No request body.

#### Possible Response Status Codes
- `200 OK`: Positions retrieved successfully.
- `503 Service Unavailable`: TWS connection lost or request timed out (IBKR mode).

#### Response Body Schema (`200 OK` — Array of `PositionSchema`)
```json
[
  {
    "symbol": "string",
    "quantity": 10,
    "average_price": "decimal-string",
    "unrealized_pnl": "decimal-string",
    "realized_pnl": "decimal-string"
  }
]
```

#### Actual Tested Response (Empty Positions)
```json
[]
```

#### Execution Metadata
- **Broker**: `IBKRBroker.get_positions()` in IBKR mode; `MockBroker.get_positions()` in Mock mode
- **TWS Required**: Yes in IBKR mode (requests `reqPositions` from TWS socket)
- **Safe to Execute Manually**: ✅ Yes (Read-only)

---

### Endpoint 5: Get All Orders (Order Book)

#### Method
`GET`

#### Full URL
`http://127.0.0.1:8000/api/v1/orders`

#### Purpose
Retrieve all tracked orders (regardless of status) from the active broker.

#### Headers
None

#### Path Parameters
None

#### Query Parameters
None

#### Request Body
No request body.

#### Possible Response Status Codes
- `200 OK`: Orders retrieved successfully.
- `503 Service Unavailable`: TWS connection lost (IBKR mode).

#### Response Body Schema (`200 OK` — Array of `OrderSchema`)
```json
[
  {
    "order_id": "string",
    "symbol": "string",
    "side": "BUY | SELL",
    "quantity": 1,
    "order_type": "MARKET | LIMIT",
    "status": "PENDING | SUBMITTED | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED",
    "timestamp": "ISO-8601 datetime",
    "price": "decimal-string | null",
    "filled_quantity": 0,
    "average_fill_price": "decimal-string | null"
  }
]
```

#### Actual Tested Response (After Order Placement)
```json
[
  {
    "order_id": "1",
    "symbol": "AAPL",
    "side": "BUY",
    "quantity": 1,
    "order_type": "LIMIT",
    "status": "REJECTED",
    "timestamp": "2026-08-10T08:51:48.962516Z",
    "price": "1.00",
    "filled_quantity": 0,
    "average_fill_price": null
  }
]
```

#### Execution Metadata
- **Broker**: `IBKRBroker.get_order_book()` in IBKR mode; `MockBroker.get_order_book()` in Mock mode
- **TWS Required**: Yes in IBKR mode
- **Safe to Execute Manually**: ✅ Yes (Read-only)

---

### Endpoint 6: Place New Order

#### Method
`POST`

#### Full URL
`http://127.0.0.1:8000/api/v1/orders`

#### Purpose
Transmit a new order to the active broker (TWS socket in IBKR mode or in-memory state in Mock mode).

#### Headers
`Content-Type: application/json`

#### Path Parameters
None

#### Query Parameters
None

#### Request Body Schema (`PlaceOrderRequest`)
```json
{
  "symbol": "AAPL",
  "side": "BUY",
  "quantity": 1,
  "order_type": "LIMIT",
  "price": "1.00"
}
```

#### Request Schema Rules
- `symbol` (string, required): Non-empty asset ticker.
- `side` (string, required): Must be `"BUY"` or `"SELL"`.
- `quantity` (integer, required): Must be `> 0`.
- `order_type` (string, required): Must be `"MARKET"` or `"LIMIT"`.
- `price` (string/number, optional): Required for `"LIMIT"` orders (must be `> 0`). Set to `null` for `"MARKET"` orders.

#### Possible Response Status Codes
- `200 OK`: Order placed and initial order object returned.
- `400 Bad Request`: Invalid parameter (e.g. non-positive quantity, missing limit price).
- `422 Unprocessable Entity`: Schema/type validation error.
- `503 Service Unavailable`: TWS disconnected (IBKR mode).

#### Actual Tested Response (`200 OK` — IBKR Mode on Live TWS)
```json
{
  "order_id": "1",
  "symbol": "AAPL",
  "side": "BUY",
  "quantity": 1,
  "order_type": "LIMIT",
  "status": "PENDING",
  "timestamp": "2026-08-10T08:51:48.962516Z",
  "price": "1.00",
  "filled_quantity": 0,
  "average_fill_price": null
}
```

#### Execution Metadata
- **Broker**: `IBKRBroker.place_order()` → `TWSClient.placeOrder()` in IBKR mode; `MockBroker.place_order()` in Mock mode
- **TWS Required**: Yes in IBKR mode
- **Safe to Execute Manually**: ⚠️ **Use with caution in IBKR mode.** Place a LIMIT order away from market price on a paper account to avoid accidental execution.

---

### Endpoint 7: Modify Open Order

#### Method
`PUT`

#### Full URL
`http://127.0.0.1:8000/api/v1/orders/{order_id}`

#### Purpose
Modify the quantity or limit price of an open order.

#### Headers
`Content-Type: application/json`

#### Path Parameters
| Parameter | Type | Required | Example | Description |
|:---|:---|:---|:---|:---|
| `order_id` | string | Yes | `"1"` | Order ID assigned during placement |

#### Query Parameters
None

#### Request Body Schema (`ModifyOrderRequest`)
```json
{
  "quantity": 2,
  "price": "1.05"
}
```

#### Possible Response Status Codes
- `200 OK`: Order modification submitted to broker.
- `400 Bad Request`: Order is in terminal state (`FILLED`, `CANCELLED`, `REJECTED`) or price/qty invalid.
- `404 Not Found`: Order ID does not exist in order book.
- `422 Unprocessable Entity`: Validation error.
- `503 Service Unavailable`: TWS disconnected (IBKR mode).

#### Response Body Schema (`200 OK` — `OrderSchema`)
```json
{
  "order_id": "1",
  "symbol": "AAPL",
  "side": "BUY",
  "quantity": 2,
  "order_type": "LIMIT",
  "status": "SUBMITTED",
  "timestamp": "2026-08-10T08:51:48.962516Z",
  "price": "1.05",
  "filled_quantity": 0,
  "average_fill_price": null
}
```

#### Actual Tested Error Response (`400 Bad Request` — Terminal Order)
```json
{
  "detail": "Cannot modify order in terminal state: REJECTED"
}
```

#### Execution Metadata
- **Broker**: `IBKRBroker.modify_order()` in IBKR mode; `MockBroker.modify_order()` in Mock mode
- **TWS Required**: Yes in IBKR mode
- **Safe to Execute Manually**: ⚠️ Yes (Applies only to existing open orders)

---

### Endpoint 8: Cancel Order

#### Method
`DELETE`

#### Full URL
`http://127.0.0.1:8000/api/v1/orders/{order_id}`

#### Purpose
Submit a cancellation request for an open order.

#### Headers
None

#### Path Parameters
| Parameter | Type | Required | Example | Description |
|:---|:---|:---|:---|:---|
| `order_id` | string | Yes | `"1"` | Order ID to cancel |

#### Query Parameters
None

#### Request Body
No request body.

#### Possible Response Status Codes
- `200 OK`: Cancellation request submitted.
- `400 Bad Request`: Order is already in a terminal state (`FILLED`, `CANCELLED`, `REJECTED`).
- `404 Not Found`: Order ID does not exist.
- `503 Service Unavailable`: TWS disconnected (IBKR mode).

#### Response Body Schema (`200 OK` — `OrderSchema`)
```json
{
  "order_id": "1",
  "symbol": "AAPL",
  "side": "BUY",
  "quantity": 1,
  "order_type": "LIMIT",
  "status": "SUBMITTED",
  "timestamp": "2026-08-10T08:51:48.962516Z",
  "price": "1.00",
  "filled_quantity": 0,
  "average_fill_price": null
}
```

#### Execution Metadata
- **Broker**: `IBKRBroker.cancel_order()` → `TWSClient.cancelOrder()` in IBKR mode; `MockBroker.cancel_order()` in Mock mode
- **TWS Required**: Yes in IBKR mode
- **Safe to Execute Manually**: ✅ Yes (Safe cancel operation)

---

### Endpoint 9: Submit Market Data Tick (Manual Ingestion)

#### Method
`POST`

#### Full URL
`http://127.0.0.1:8000/api/v1/market-data`

#### Purpose
Manually inject a synthetic market data tick into the `TradingService` → `CandleBuilder` → `FiveCandleStrategy` → `OrderManager` pipeline.

#### Headers
`Content-Type: application/json`

#### Path Parameters
None

#### Query Parameters
None

#### Request Body Schema (`MarketDataEventRequest`)
```json
{
  "timestamp": "2025-06-15T10:00:00Z",
  "price": "100.50",
  "volume": 50
}
```

#### Request Schema Rules
- `timestamp` (string, required): ISO-8601 string. **Must be timezone-aware** (include `Z` or UTC offset like `+00:00`).
- `price` (number/string, required): Observed price (must be `> 0`).
- `volume` (integer, required): Trading volume (must be `≥ 0`).

#### Possible Response Status Codes
- `200 OK`: Tick ingested successfully.
- `400 Bad Request`: Naive timestamp provided (missing timezone).
- `422 Unprocessable Entity`: Validation error (negative price, negative volume, missing required field).

#### Response Body Schema (`200 OK` — Incomplete Candle)
```json
{
  "candle_completed": false,
  "signal": null,
  "order": null
}
```

#### Response Body Schema (`200 OK` — Candle Completed with BUY Signal)
```json
{
  "candle_completed": true,
  "signal": {
    "signal_type": "BUY",
    "timestamp": "2026-08-10T08:00:14.362082Z",
    "reason": "Five consecutive bullish candles"
  },
  "order": {
    "order_id": "MOCK-0F9B0EE2",
    "symbol": "RELIANCE",
    "side": "BUY",
    "quantity": 1,
    "order_type": "MARKET",
    "status": "SUBMITTED",
    "timestamp": "2026-08-10T08:00:14.362359Z",
    "price": null,
    "filled_quantity": 0,
    "average_fill_price": null
  }
}
```

#### Execution Metadata
- **Broker**: Active broker (placed via `OrderManager`)
- **TWS Required**: No (works in both Mock and IBKR modes)
- **Safe to Execute Manually**: ✅ Yes (Synthetic data input)

---

### Endpoint 10: Subscribe to TWS Market Data

#### Method
`POST`

#### Full URL
`http://127.0.0.1:8000/api/v1/market-data/subscribe`

#### Purpose
Request TWS live market data subscription for the configured symbol (`IBKR_MARKET_DATA_SYMBOL`). Ticks automatically stream to `IBKRMarketDataAdapter` and feed the candle pipeline via background consumer task.

#### Headers
None

#### Path Parameters
None

#### Query Parameters
None

#### Request Body
No request body.

#### Possible Response Status Codes
- `200 OK`: Subscription requested successfully.
- `400 Bad Request`: Called while running in Mock Mode (`BROKER_MODE=mock`).
- `503 Service Unavailable`: TWS client disconnected.

#### Response Body Schema (`200 OK` — `MarketDataSubscriptionResponse`)
```json
{
  "subscribed": true,
  "symbol": "AAPL",
  "request_id": 10000000
}
```

#### Actual Tested Response (IBKR Mode with TWS)
```json
{
  "subscribed": true,
  "symbol": "AAPL",
  "request_id": 10000000
}
```

#### Actual Tested Response (Mock Mode Error)
```json
{
  "detail": "Market data subscription is only available in IBKR mode."
}
```

#### Execution Metadata
- **Broker**: `IBKRMarketDataAdapter.request_market_data()`
- **TWS Required**: Yes (IBKR Mode only)
- **Safe to Execute Manually**: ✅ Yes

---

### Endpoint 11: Cancel TWS Market Data Subscription

#### Method
`DELETE`

#### Full URL
`http://127.0.0.1:8000/api/v1/market-data/subscribe`

#### Purpose
Cancel active TWS market data subscription.

#### Headers
None

#### Path Parameters
None

#### Query Parameters
None

#### Request Body
No request body.

#### Possible Response Status Codes
- `200 OK`: Subscription cancelled.
- `400 Bad Request`: Called while running in Mock Mode.

#### Response Body Schema (`200 OK` — `MarketDataSubscriptionResponse`)
```json
{
  "subscribed": false,
  "symbol": null,
  "request_id": null
}
```

#### Actual Tested Response
```json
{
  "subscribed": false,
  "symbol": null,
  "request_id": null
}
```

#### Execution Metadata
- **Broker**: `IBKRMarketDataAdapter.cancel_market_data()`
- **TWS Required**: Yes (IBKR Mode only)
- **Safe to Execute Manually**: ✅ Yes

---

## 5. Standard Error Structures

### Pydantic Validation Error (`422 Unprocessable Entity`)

Returned when request body JSON fields fail schema validation.

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "timestamp"
      ],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

### Domain Error (`400 Bad Request` / `404 Not Found` / `503 Service Unavailable`)

Returned when client requests invalid business operation or resources do not exist.

```json
{
  "detail": "Human-readable error explanation message"
}
```

---

## 6. Complete Postman Testing Execution Sequence

Follow this step-by-step sequence in Postman to test the full IBKR paper trading system:

1. **Check System Health**:
   - `GET http://127.0.0.1:8000/health`
2. **Verify Broker Mode & Connection Status**:
   - `GET http://127.0.0.1:8000/api/v1/broker/status`
   - *Ensure `connected: true` and `broker_mode: "ibkr"` (or `"mock"`).*
3. **Inspect Account Margin**:
   - `GET http://127.0.0.1:8000/api/v1/margin`
4. **Inspect Initial Positions**:
   - `GET http://127.0.0.1:8000/api/v1/positions`
5. **Place Safe Test Order**:
   - `POST http://127.0.0.1:8000/api/v1/orders`
   - Body: `{"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT", "price": "1.00"}`
6. **Verify Order Book**:
   - `GET http://127.0.0.1:8000/api/v1/orders`
7. **Modify Order**:
   - `PUT http://127.0.0.1:8000/api/v1/orders/{order_id}`
   - Body: `{"price": "1.05"}`
8. **Cancel Order**:
   - `DELETE http://127.0.0.1:8000/api/v1/orders/{order_id}`
9. **Subscribe to Live Market Data Feed**:
   - `POST http://127.0.0.1:8000/api/v1/market-data/subscribe`
10. **Cancel Market Data Feed**:
    - `DELETE http://127.0.0.1:8000/api/v1/market-data/subscribe`
