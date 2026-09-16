# 04-DOMAIN-MODELS.md — Domain Models, Relationships & Identifiers

---

## 1. CORE DOMAIN ENTITIES & RELATIONSHIPS

```
                      ┌────────────────┐
                      │  AccountModel  │
                      └───────┬────────┘
                              │ 1:N
        ┌─────────────────────┼─────────────────────┐
        │ 1:N                 │ 1:N                 │ 1:N
        ▼                     ▼                     ▼
┌──────────────┐      ┌──────────────┐      ┌──────────────────┐
│  OrderModel  │      │PositionModel │      │ ManualOrderModel │
└───────┬──────┘      └──────────────┘      └─────────┬────────┘
        │ 1:N (Engine)                                │ 1:N (Manual)
        ▼                                             ▼
┌────────────────┐                          ┌─────────────────────┐
│ ExecutionModel │                          │ManualExecutionModel │
└────────────────┘                          └─────────────────────┘
        │                                             │
        └─────────────────────┬───────────────────────┘
                              │ Mirrors to Trade Book
                              ▼
                   ┌─────────────────────┐
                   │ TradeExecutionModel │
                   └─────────────────────┘
```

### Entity Descriptions
- **`AccountModel` (`accounts`)**: Models the trading account, containing account-level margin rules, risk limits, daily targets, daily stops, and pause state.
- **`SignalJobModel` (`signal_jobs`)**: Durable queue item representing an inbound signal alert. Tracks worker leases (`lease_expires_at`) and retry attempts.
- **`SignalModel` (`signals`)**: Permanent record of raw inbound webhook signals.
- **`ExecutionClaimModel` (`execution_claims`)**: Deduplication barrier guaranteeing at-most-once execution submission per `(strategy, signal_id, action, account_id)`.
- **`OrderModel` (`orders`)**: Engine child orders submitted to IBKR.
- **`ManualOrderModel` (`manual_orders`)**: User-submitted manual CFD orders. Contains two-phase commit status (`PENDING_SUBMIT` $\rightarrow$ `SUBMITTED`).
- **`ExecutionModel` (`executions`)**: Engine fills deduplicated on `exec_id`.
- **`ManualExecutionModel` (`manual_executions`)**: Manual fills deduplicated on `exec_id`.
- **`TradeExecutionModel` (`trade_executions`)**: Unified Trade Book ledger synchronizing all same-day broker executions.
- **`PositionModel` (`positions`)**: Engine pair-level position tracking Leg A and Leg B. Composite PK: `(account_id, trade_id)`.
- **`ManualPositionModel` (`manual_positions`)**: Manual lot position tracking net signed quantity, average cost, and realized PnL. Unique constraint: `(account_id, trade_id)`.
- **`BrokerPositionModel` (`broker_positions`)**: Periodic broker physical position snapshot. PK: `(ibkr_account, con_id)`.
- **`BasketModel` (`baskets`)**: Multi-leg execution state machine. Tracks `OPEN`, `CLOSED`, `COMPENSATING`, `CRITICAL`.
- **`KillSwitchOperationModel` (`kill_switch_operations`)**: Durable emergency flatten operations tracking status from `ACTIVATING` to `COMPLETE` and `CLEARED`.

---

## 2. THE CANONICAL IDENTIFIER MODEL

| Identifier | Column / Field | Scope & Uniqueness | Semantics & Lifetime |
|---|---|---|---|
| **Account ID** | `accounts.id` | Internal Numeric (BigInt) | Primary key of the local account. Never send to IBKR. |
| **IBKR Account** | `ibkr_account` | String (e.g. `DU123456`) | External broker account identifier. Used in API requests and callbacks. |
| **Internal Order ID** | `internal_order_id` | String (`ORD-...` or `MAN-...`) | Globally unique order tracking token. Passed to IBKR as `ib_order.orderRef`. |
| **Broker Order ID** | `broker_order_id` | String / Integer | Ephemeral session order ID from `client.allocate_next_order_id()`. Invalid across gateway restarts. |
| **Permanent Order ID**| `perm_id` | BigInt | Authoritative broker order ID minted by IBKR. Valid across gateway reconnects. |
| **Trade ID** | `trade_id` | String (`TRD_...`) | Identifies an economic trade/position lot. **MUST NEVER BE REUSED ONCE CLOSED**. |
| **Signal ID** | `signal_id` | String | Strategy alert identifier from TradingView. |
| **Execution ID** | `exec_id` | String | Globally unique broker execution ID. Idempotency key for fills. |
| **Contract ID** | `con_id` | BigInt | Unique security identifier at IBKR. CFD `con_id` is distinct from STK `con_id`. |
| **Dedupe Key** | `dedupe_key` | String (SHA256) | Hash of strategy, signal, action, and account. Pre-submission lock. |

---

## 3. STRICT IDENTITY CORRELATION RULES

1. **Never Correlate by Symbol Alone**: In multi-account or multi-lot environments, multiple orders or positions share the same symbol. Always correlate via `orderRef` (internal_order_id), `perm_id`, or `(account_id, trade_id)`.
2. **Never Confuse `trade_id` with `internal_order_id`**: A single `trade_id` (position lot) can be affected by multiple orders (an initial open order, an adding order, and a closing order).
3. **Never Synthesize an `exec_id`**: The broker's `execId` string is the sole authoritative proof of fill.
