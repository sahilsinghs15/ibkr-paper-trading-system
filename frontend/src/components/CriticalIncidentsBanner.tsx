import { Link } from 'react-router-dom'
import type { CriticalBasketRow } from '../types/criticalBaskets'

function recoveryLabel(status: string | null): string {
  switch (status) {
    case 'RECOVERING':
      return 'Recovering'
    case 'FAILED':
      return 'Recovery failed'
    case 'CLEARED':
      return 'Cleared'
    default:
      return 'Critical'
  }
}

function recoveryBadgeClass(status: string | null): string {
  switch (status) {
    case 'RECOVERING':
      return 'critical-recovery-badge recovering'
    case 'FAILED':
      return 'critical-recovery-badge failed'
    default:
      return 'critical-recovery-badge critical'
  }
}

interface CriticalIncidentsBannerProps {
  ibkrAccount: string
  incidents: CriticalBasketRow[]
}

export function CriticalIncidentsBanner({
  ibkrAccount,
  incidents,
}: CriticalIncidentsBannerProps) {
  if (incidents.length === 0) {
    return null
  }

  const strategies = [...new Set(incidents.map((i) => i.strategy_id))]

  return (
    <div className="critical-incidents-panel" role="alert">
      <div className="critical-incidents-header">
        <strong>CRITICAL basket incident — new OPENs blocked</strong>
        <span className="critical-incidents-sub">
          Strategy{strategies.length > 1 ? 'ies' : ''}: {strategies.join(', ')}. Auto-flatten
          recovery is running; OPENs resume when this list is empty.
        </span>
      </div>
      <table className="critical-incidents-table">
        <thead>
          <tr>
            <th>Trade</th>
            <th>Action</th>
            <th>Recovery</th>
            <th>Legs (filled / intended)</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {incidents.map((inc) => (
            <tr key={`${inc.trade_id}-${inc.action}`}>
              <td className="mono">{inc.trade_id}</td>
              <td>{inc.action}</td>
              <td>
                <span className={recoveryBadgeClass(inc.recovery_status)}>
                  {recoveryLabel(inc.recovery_status)}
                </span>
              </td>
              <td>
                {inc.legs.length === 0 ? (
                  '—'
                ) : (
                  <ul className="critical-legs-list">
                    {inc.legs.map((leg) => (
                      <li key={`${inc.trade_id}-${leg.leg}`}>
                        {leg.symbol}: {leg.filled_qty} / {leg.intended_qty}
                      </li>
                    ))}
                  </ul>
                )}
              </td>
              <td className="critical-detail-cell">{inc.recovery_detail ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="critical-incidents-footer">
        Broker orphans may appear on{' '}
        <Link to={`/account/${ibkrAccount}/reconcile`}>Reconcile</Link> until the snapshot is
        flat.
      </p>
    </div>
  )
}
