import { Routes, Route } from 'react-router-dom'
import { PositionsPage } from './pages/PositionsPage'
import { SettingsPage } from './pages/SettingsPage'
import './App.css'

function App() {
  return (
    <Routes>
      <Route path="/" element={<PositionsPage />} />
      <Route path="/settings" element={<SettingsPage />} />
    </Routes>
  )
}

export default App
