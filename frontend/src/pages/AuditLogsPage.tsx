import { useSearchParams } from 'react-router-dom'
import { OperatorAuditTrail } from '../components/audit/OperatorAuditTrail'
import { SystemEventJournal } from '../components/audit/SystemEventJournal'

type Tab = 'trail' | 'journal'

export function AuditLogsPage() {
  const [params, setParams] = useSearchParams()
  const tab: Tab = params.get('tab') === 'journal' ? 'journal' : 'trail'

  const select = (next: Tab) => {
    const p = new URLSearchParams(params)
    if (next === 'trail') p.delete('tab')
    else p.set('tab', next)
    setParams(p, { replace: true })
  }

  return (
    <main className="page audit-page">
      <header className="audit-page-head">
        <div>
          <h1>Audit &amp; Accountability</h1>
          <span className="audit-muted">
            Server-recorded operator actions that affect trading, risk, configuration, inventory,
            emergency state, services and security — with authenticated identity and observed
            session/network context.
          </span>
        </div>
      </header>

      <div className="audit-tabs" role="tablist" aria-label="Audit views">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'trail'}
          className={`history-filter-btn ${tab === 'trail' ? 'active' : ''}`}
          onClick={() => select('trail')}
        >
          Operator Audit Trail
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'journal'}
          className={`history-filter-btn ${tab === 'journal' ? 'active' : ''}`}
          onClick={() => select('journal')}
        >
          System Event Journal
        </button>
      </div>

      {tab === 'trail' ? <OperatorAuditTrail /> : <SystemEventJournal />}
    </main>
  )
}
