import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchCriticalBaskets } from '../api/criticalBasketsApi'
import { ClosedPositionsTable } from '../components/ClosedPositionsTable'
import { CriticalIncidentsBanner } from '../components/CriticalIncidentsBanner'
import { Kpis } from '../components/Kpis'
import { OpenPositionsTable } from '../components/OpenPositionsTable'
import { SignalTrayTable } from '../components/SignalTrayTable'
import { SignalWidget } from '../components/SignalWidget'
import { useSignalStore } from '../store/signalStore'
import type { CriticalBasketRow } from '../types/criticalBaskets'
import { normalizeIbkrAccount } from '../utils/activeAccount'

export function PositionsPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount)
  const [activeTab, setActiveTab] = useState<'signals' | 'open' | 'closed'>('open')
  const [criticalIncidents, setCriticalIncidents] = useState<CriticalBasketRow[]>([])
  const fetchSignals = useSignalStore((s) => s.fetchSignals)

  const loadCritical = useCallback(async () => {
    if (!cleanAccount) return
    try {
      const res = await fetchCriticalBaskets(cleanAccount)
      setCriticalIncidents(res.incidents)
    } catch {
      setCriticalIncidents([])
    }
  }, [cleanAccount])

  useEffect(() => {
    void fetchSignals({ account: cleanAccount })
  }, [cleanAccount, fetchSignals])

  useEffect(() => {
    void loadCritical()
    const timer = setInterval(() => {
      void loadCritical()
    }, 5000)
    return () => clearInterval(timer)
  }, [loadCritical])

  return (
    <main className="page dashboard-layout">
      {/* Left Column: Compact Signal Monitor Widget */}
      <SignalWidget
        accountFilter={cleanAccount}
        onViewFullTray={() => setActiveTab('signals')}
      />

      {/* Main Dashboard Column */}
      <section className="dashboard-main-column">
        <Kpis accountFilter={cleanAccount} />

        <CriticalIncidentsBanner
          ibkrAccount={cleanAccount ?? ''}
          incidents={criticalIncidents}
        />

        {/* Workspace Navigation Tabs */}
        <div className="dashboard-tabs-header">
          <div className="tab-group" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === 'signals'}
              className={`tab-btn ${activeTab === 'signals' ? 'active' : ''}`}
              onClick={() => setActiveTab('signals')}
            >
              SIGNAL TRAY
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === 'open'}
              className={`tab-btn ${activeTab === 'open' ? 'active' : ''}`}
              onClick={() => setActiveTab('open')}
            >
              OPEN POSITIONS
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === 'closed'}
              className={`tab-btn ${activeTab === 'closed' ? 'active' : ''}`}
              onClick={() => setActiveTab('closed')}
            >
              RECENTLY CLOSED
            </button>
          </div>
        </div>

        {/* Tab Workspace Content */}
        <div className="dashboard-tab-content">
          {activeTab === 'signals' ? (
            <SignalTrayTable accountFilter={cleanAccount} />
          ) : activeTab === 'open' ? (
            <OpenPositionsTable accountFilter={cleanAccount} />
          ) : (
            <ClosedPositionsTable accountFilter={cleanAccount} />
          )}
        </div>
      </section>
    </main>
  )
}
