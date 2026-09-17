# 13-FRONTEND.md — Frontend Architecture & State Boundary

---

## 1. REACT ARCHITECTURE & PAGES

The frontend is a Single Page Application (SPA) built with React 18, TypeScript, and Vite under [`frontend/`](file:///home/dev3/Documents/ibkr-paper-trading-system/frontend):

### Major Pages
- **`LoginPage` (`/login`)**: User authentication.
- **`AccountsPage` (`/accounts`)**: Admin account list and global metrics.
- **`PositionsPage` (`/account/:ibkrAccount`)**: Engine pair positions, live PnL, manual pair close buttons.
- **`ManualTradePage` (`/account/:ibkrAccount/manual-trade`)**: CFD order entry form, what-if margin modal, order submission ticket.
- **`ManualPositionsPage` (`/account/:ibkrAccount/manual-trade/positions`)**: Manual lot table with real-time PnL, close position action.
- **`OrderBookPage` (`/account/:ibkrAccount/order-book`)**: Engine & manual orders with source badges.
- **`TradeBookPage` (`/account/:ibkrAccount/trade-book`)**: Execution fills ledger with sync status.
- **`ReconcilePage` (`/account/:ibkrAccount/reconcile`)**: Broker vs Ledger comparison matrix and mismatch resolution.
- **`SystemMonitorPage` (`/account/:ibkrAccount/system-monitor`)**: Host telemetry and AWS daily credit ledger.
- **`AccountSettingsPage` (`/account/:ibkrAccount/settings`)**: Risk parameters, daily stops, trading pause, kill switch.

---

## 2. STATE & STREAMING HOOKS

1. **`usePnlStream` (`frontend/src/hooks/usePnlStream.ts`)**:
   - Opens an `EventSource` connection to `/demo/stream`.
   - Streams live position updates and mark-driven PnL calculations.
2. **`useSystemEvents` (`frontend/src/hooks/useSystemEvents.ts`)**:
   - Streams system events and triggers desktop/toast notifications.
3. **`useAuthStore` (`frontend/src/store/authStore.ts`)**:
   - Zustand store holding user tokens, active account code, and admin permissions.

---

## 3. THE FRONTEND/BACKEND BOUNDARY

- **Zero Business Logic in the UI**: Frontend forms perform basic input formatting only.
- **All Safety Gates Backend**: The backend validates price, ticks, margin, halts, pause, and kill switch upon submit.
- **Idempotency Key Generation**: The UI generates UUIDv4 tokens for `idempotency_key` (falling back to polyfill if `crypto.randomUUID` is unavailable on insecure HTTP).
