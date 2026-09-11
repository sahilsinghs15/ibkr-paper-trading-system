import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { AccountScoped } from './components/AccountScoped'
import { AppHeader } from './components/AppHeader'
import { ProtectedRoute } from './components/ProtectedRoute'
import { RedirectToAccount } from './components/RedirectToAccount'
import { LoginPage } from './pages/LoginPage'
import { AccountsPage } from './pages/AccountsPage'
import { PositionsPage } from './pages/PositionsPage'
import { AccountSettingsPage } from './pages/AccountSettingsPage'
import { SystemMonitorPage } from './pages/SystemMonitorPage'
import { ReconcilePage } from './pages/ReconcilePage'
import { TradeBookPage } from './pages/TradeBookPage'
import { OrderBookPage } from './pages/OrderBookPage'
import { AuditLogsPage } from './pages/AuditLogsPage'
import { IngestFeedPage } from './pages/IngestFeedPage'
import { ManualTradePage } from './pages/ManualTradePage'
import { ManualPositionsPage } from './pages/ManualPositionsPage'
import { NotificationContainer } from './components/NotificationContainer'
import { usePnlStream } from './hooks/usePnlStream'
import { useSystemEvents } from './hooks/useSystemEvents'
import { useAuthStore } from './store/authStore'
import './App.css'

function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const user = useAuthStore((s) => s.user)
  const location = useLocation()

  usePnlStream()
  useSystemEvents()

  const hideHeader = location.pathname === '/login'

  return (
    <>
      <NotificationContainer />
      {!hideHeader && <AppHeader />}
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/accounts"
          element={
            <ProtectedRoute requireAdmin>
              <AccountsPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount"
          element={
            <ProtectedRoute>
              <AccountScoped>
                <PositionsPage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/settings"
          element={
            <ProtectedRoute>
              <AccountScoped>
                <AccountSettingsPage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/system-monitor"
          element={
            <ProtectedRoute requireAdmin>
              <AccountScoped>
                <SystemMonitorPage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/audit-logs"
          element={
            <ProtectedRoute requireAdmin>
              <AccountScoped>
                <AuditLogsPage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/ingest"
          element={
            <ProtectedRoute requireAdmin>
              <AccountScoped>
                <IngestFeedPage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/reconcile"
          element={
            <ProtectedRoute>
              <AccountScoped>
                <ReconcilePage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/trade-book"
          element={
            <ProtectedRoute>
              <AccountScoped>
                <TradeBookPage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/order-book"
          element={
            <ProtectedRoute>
              <AccountScoped>
                <OrderBookPage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/manual-trade"
          element={
            <ProtectedRoute>
              <AccountScoped>
                <ManualTradePage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/account/:ibkrAccount/manual-trade/positions"
          element={
            <ProtectedRoute>
              <AccountScoped>
                <ManualPositionsPage />
              </AccountScoped>
            </ProtectedRoute>
          }
        />
        <Route
          path="/manual-trade"
          element={
            <ProtectedRoute>
              <RedirectToAccount suffix="/manual-trade" />
            </ProtectedRoute>
          }
        />
        <Route
          path="/manual-trade/positions"
          element={
            <ProtectedRoute>
              <RedirectToAccount suffix="/manual-trade/positions" />
            </ProtectedRoute>
          }
        />
        <Route
          path="/trade-book"
          element={
            <ProtectedRoute>
              <RedirectToAccount suffix="/trade-book" />
            </ProtectedRoute>
          }
        />
        <Route
          path="/order-book"
          element={
            <ProtectedRoute>
              <RedirectToAccount suffix="/order-book" />
            </ProtectedRoute>
          }
        />
        <Route
          path="/settings"
          element={
            <ProtectedRoute>
              <RedirectToAccount suffix="/settings" />
            </ProtectedRoute>
          }
        />
        <Route
          path="/system-monitor"
          element={
            <ProtectedRoute requireAdmin>
              <RedirectToAccount suffix="/system-monitor" />
            </ProtectedRoute>
          }
        />
        <Route
          path="/audit-logs"
          element={
            <ProtectedRoute requireAdmin>
              <RedirectToAccount suffix="/audit-logs" />
            </ProtectedRoute>
          }
        />
        <Route
          path="/ingest"
          element={
            <ProtectedRoute requireAdmin>
              <RedirectToAccount suffix="/ingest" />
            </ProtectedRoute>
          }
        />
        <Route
          path="/"
          element={
            !isAuthenticated ? (
              <Navigate to="/login" replace />
            ) : user?.role === 'user' && user.ibkr_account ? (
              <Navigate to={`/account/${user.ibkr_account}`} replace />
            ) : (
              <Navigate to="/accounts" replace />
            )
          }
        />
      </Routes>
    </>
  )
}

export default App
