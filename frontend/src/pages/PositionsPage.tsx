import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { ClosedPositionsTable } from '../components/ClosedPositionsTable'
import { Kpis } from '../components/Kpis'
import { OpenPositionsTable } from '../components/OpenPositionsTable'
import { SignalTray } from '../components/SignalTray'

export function PositionsPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = ibkrAccount ? ibkrAccount.trim().toUpperCase() : 'DUR919062'
  const [activeTab, setActiveTab] = useState<'open' | 'closed'>('open')

  return (
    <main className="page dashboard-layout">
      <SignalTray accountFilter={cleanAccount} />

      <section className="dashboard-main-column">
        <Kpis accountFilter={cleanAccount} />

        <div className="dashboard-tabs-header">
          <div className="tab-group" role="tablist">
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

        <div className="dashboard-tab-content">
          {activeTab === 'open' ? (
            <OpenPositionsTable accountFilter={cleanAccount} />
          ) : (
            <ClosedPositionsTable accountFilter={cleanAccount} />
          )}
        </div>
      </section>
    </main>
  )
}
