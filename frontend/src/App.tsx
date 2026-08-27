import { Routes, Route, Navigate } from 'react-router-dom'
import { AccountScoped } from './components/AccountScoped'
import { AppHeader } from './components/AppHeader'
import { RedirectToAccount } from './components/RedirectToAccount'
import { AccountsPage } from './pages/AccountsPage'
import { PositionsPage } from './pages/PositionsPage'
import { AccountSettingsPage } from './pages/AccountSettingsPage'
import { SystemMonitorPage } from './pages/SystemMonitorPage'
import { ReconcilePage } from './pages/ReconcilePage'
import { usePnlStream } from './hooks/usePnlStream'
import './App.css'

function App() {
  usePnlStream()

  return (
    <>
      <AppHeader />
      <Routes>
        <Route path="/accounts" element={<AccountsPage />} />
        <Route
          path="/account/:ibkrAccount"
          element={
            <AccountScoped>
              <PositionsPage />
            </AccountScoped>
          }
        />
        <Route
          path="/account/:ibkrAccount/settings"
          element={
            <AccountScoped>
              <AccountSettingsPage />
            </AccountScoped>
          }
        />
        <Route
          path="/account/:ibkrAccount/system-monitor"
          element={
            <AccountScoped>
              <SystemMonitorPage />
            </AccountScoped>
          }
        />
        <Route
          path="/account/:ibkrAccount/reconcile"
          element={
            <AccountScoped>
              <ReconcilePage />
            </AccountScoped>
          }
        />
        <Route path="/settings" element={<RedirectToAccount suffix="/settings" />} />
        <Route path="/system-monitor" element={<RedirectToAccount suffix="/system-monitor" />} />
        <Route path="/" element={<Navigate to="/accounts" replace />} />
      </Routes>
    </>
  )
}

export default App
