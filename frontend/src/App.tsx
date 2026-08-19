import { Routes, Route, Navigate } from 'react-router-dom'
import { AppHeader } from './components/AppHeader'
import { AccountsPage } from './pages/AccountsPage'
import { PositionsPage } from './pages/PositionsPage'
import { AccountSettingsPage } from './pages/AccountSettingsPage'
import { usePnlStream } from './hooks/usePnlStream'
import './App.css'

function App() {
  usePnlStream()

  return (
    <>
      <AppHeader />
      <Routes>
        <Route path="/accounts" element={<AccountsPage />} />
        <Route path="/account/:ibkrAccount" element={<PositionsPage />} />
        <Route path="/account/:ibkrAccount/settings" element={<AccountSettingsPage />} />
        <Route path="/settings" element={<Navigate to="/account/DUR919062/settings" replace />} />
        <Route path="/" element={<Navigate to="/accounts" replace />} />
      </Routes>
    </>
  )
}

export default App
