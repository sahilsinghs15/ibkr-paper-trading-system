import { Navigate, useLocation, useSearchParams } from 'react-router-dom'
import { OperatorAuditTrail } from '../components/audit/OperatorAuditTrail'

export function AuditLogsPage() {
  const [params] = useSearchParams()
  const location = useLocation()

  // The machine/system event journal now lives on the System Monitor page.
  if (params.get('tab') === 'journal') {
    const target = location.pathname.replace(/\/audit-logs$/, '/system-monitor')
    return <Navigate to={`${target}#event-journal`} replace />
  }

  return (
    <main className="page audit-page">
      <header className="audit-page-head">
        <h1>Audit &amp; Accountability</h1>
        <span className="audit-muted">
          Who did what, to which account, and with what result — recorded by the server for actions
          that affect trading, risk, settings, inventory, emergency controls, services and security.
        </span>
      </header>
      <OperatorAuditTrail />
    </main>
  )
}
