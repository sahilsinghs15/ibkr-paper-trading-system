# 07-POSITION-PNL.md — Position Accounting & P&L Mechanics

---

## 1. DUAL POSITION LEDGER MODELS

The repository maintains two independent authoritative position ledgers:

| Model | Table | Granularity | Key Identifiers | Structure |
|---|---|---|---|---|
| **`PositionModel`** | `positions` | Pair-level economic position | `(account_id, trade_id)` | Leg A (symbol, signed_qty, entry_mark) + Leg B (symbol, signed_qty, entry_mark) |
| **`ManualPositionModel`** | `manual_positions` | Lot-level CFD position | `(account_id, trade_id)` | Single instrument (symbol, con_id, sec_type, signed_qty, avg_cost) |

Both ledgers feed into the Expected Net Inventory (ENI) used by `PositionReconciler`.

---

## 2. MANUAL POSITION LIFECYCLE (THE 8 TRANSITIONS)

All manual position state transitions are implemented in `ManualPositionRepository.apply_execution()` under a row lock (`with_for_update()`):

```
1. Open Long (0 -> +qty):
   signed_qty = +exec_qty
   avg_cost = exec_price
   realized_pnl = -commission
   status = "OPEN"

2. Add to Long (+qty -> +qty'):
   new_qty = current_qty + exec_qty
   avg_cost = ((current_qty * current_avg) + (exec_qty * exec_price)) / new_qty
   realized_pnl = current_pnl - commission
   status = "OPEN"

3. Reduce Long (+qty -> +qty''):
   close_qty = min(current_qty, exec_qty)
   gross_pnl = close_qty * (exec_price - current_avg)
   realized_pnl = current_pnl + gross_pnl - commission
   signed_qty = current_qty - exec_qty
   avg_cost = current_avg (unchanged!)
   status = "OPEN"

4. Close Long (+qty -> 0):
   gross_pnl = current_qty * (exec_price - current_avg)
   realized_pnl = current_pnl + gross_pnl - commission
   signed_qty = 0
   status = "CLOSED"
   closed_at = now()

5. Open Short (0 -> -qty):
   signed_qty = -exec_qty
   avg_cost = exec_price
   realized_pnl = -commission
   status = "OPEN"

6. Add to Short (-qty -> -qty'):
   new_qty = abs(current_qty) + exec_qty
   avg_cost = ((abs(current_qty) * current_avg) + (exec_qty * exec_price)) / new_qty
   signed_qty = -new_qty
   realized_pnl = current_pnl - commission
   status = "OPEN"

7. Reduce Short (-qty -> -qty''):
   close_qty = min(abs(current_qty), exec_qty)
   gross_pnl = close_qty * (current_avg - exec_price)
   realized_pnl = current_pnl + gross_pnl - commission
   signed_qty = -(abs(current_qty) - exec_qty)
   avg_cost = current_avg (unchanged!)
   status = "OPEN"

8. Flip (Over-Close: Long -> Short or Short -> Long):
   - First close entire existing position at current_avg -> realize gross PnL on closed portion.
   - Remaining shares open a brand new opposite position at exec_price.
   - avg_cost becomes exec_price!
   - status = "OPEN"
```

---

## 3. UNREALIZED P&L (LIVE P&L) MECHANICS

Live unrealized PnL is computed continuously by `LivePnlService`:

### Formula
- **Single Leg**:  
  $$\text{Unrealized PnL} = \text{signed\_qty} \times (\text{Effective Mark} - \text{Entry Price / Avg Cost})$$
- **Pair Position**:  
  $$\text{Pair PnL} = \text{Unrealized Leg A} + \text{Unrealized Leg B}$$

### Effective Mark Derivation
The system derives the mark strictly from IBKR market data ticks in this order:
1. `LAST` tick price (type 4, 68).
2. Midpoint: `(BID + ASK) / 2` if both bid (1, 66) and ask (2, 67) exist.
3. `CLOSE` tick price (type 9, 75).
4. If no market ticks are available: mark is `None`. **IT NEVER USES ENTRY PRICE AS A MARK**.

### Database Throttling
PnL is computed on every incoming market tick in memory, but is flushed to PostgreSQL (`positions.live_pnl`, `manual_positions.live_pnl`) at most once per second (`_PERSIST_MIN_INTERVAL_SEC = 1.0`) to avoid database write saturation.
