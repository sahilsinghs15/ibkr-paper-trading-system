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
import { usePnlStream } from './hooks/usePnlStream'
import { useAuthStore } from './store/authStore'
import './App.css'

function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const user = useAuthStore((s) => s.user)
  const location = useLocation()

  usePnlStream()

  const hideHeader = location.pathname === '/login'

  return (
    <>
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
