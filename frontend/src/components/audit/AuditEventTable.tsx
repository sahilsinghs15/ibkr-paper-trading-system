import type { AuditEventSummary, EmptySearchFilterHint } from '../../types/audit'
import type { DisplayTimezone } from '../../types/position'
import { fmtTime } from '../../utils/format'
import { actorLabel, resultClass } from './auditFormat'

interface Props {
  items: AuditEventSummary[]
  loading: boolean
  displayTz: DisplayTimezone
  selectedId: string | null
  onSelect: (eventId: string) => void
  /** Per-filter match counts, supplied by the server when nothing matched. */
  emptyHints?: EmptySearchFilterHint[] | null
}

export function AuditEventTable({
  items,
  loading,
  displayTz,
  selectedId,
  onSelect,
  emptyHints,
}: Props) {
  // Filters that match nothing even on their own are the reason the search is
  // empty; naming them saves removing filters one at a time to find out.
  const blocking = (emptyHints ?? []).filter((h) => h.matches === 0)
  return (
    <div className="board factory-board scrollable-table-container audit-table-wrap">
      <table className="factory-table audit-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Authenticated actor</th>
            <th>Source</th>
            <th>Action</th>
            <th>Target</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody>
          {loading && items.length === 0 ? (
            <tr>
              <td colSpan={6} className="audit-empty">
                Loading audit events…
              </td>
            </tr>
          ) : items.length === 0 ? (
            <tr>
              <td colSpan={6} className="audit-empty">
                <div>No audit events match these filters.</div>
                {blocking.length > 0 && (
                  <div className="audit-empty-hint">
                    <span className="audit-empty-hint-lead">
                      {blocking.length === 1
                        ? 'This filter matches no events at all:'
                        : 'These filters match no events at all:'}
                    </span>
                    <ul>
                      {blocking.map((h) => (
                        <li key={h.field}>
                          <span className="mono">
                            {h.label} = {h.value}
                          </span>{' '}
                          — 0 events
                        </li>
                      ))}
                    </ul>
                    <span className="audit-empty-hint-lead">
                      Clear {blocking.length === 1 ? 'it' : 'them'} and search again.
                    </span>
                  </div>
                )}
                {emptyHints && emptyHints.length > 0 && blocking.length === 0 && (
                  <div className="audit-empty-hint">
                    <span className="audit-empty-hint-lead">
                      Each filter matches events on its own, so it is the combination that
                      excludes everything:
                    </span>
                    <ul>
                      {emptyHints.map((h) => (
                        <li key={h.field}>
                          <span className="mono">
                            {h.label} = {h.value}
                          </span>{' '}
                          — {h.matches} event{h.matches === 1 ? '' : 's'}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </td>
            </tr>
          ) : (
            items.map((row) => (
              <tr
                key={row.event_id}
                className={`factory-row factory-row-clickable ${selectedId === row.event_id ? 'audit-row-selected' : ''}`}
                onClick={() => onSelect(row.event_id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelect(row.event_id)
                  }
                }}
                tabIndex={0}
              >
                <td className="mono audit-nowrap">{fmtTime(row.occurred_at, displayTz)}</td>
                <td>
                  <div className="audit-actor">{actorLabel(row)}</div>
                  <div className="audit-sub">
                    {row.actor_role && <span className="audit-tag">{row.actor_role}</span>}
                    {row.provenance !== 'NATIVE' && <span className="audit-tag legacy">legacy</span>}
                  </div>
                </td>
                <td>
                  <div className="mono">{row.client_ip ?? '—'}</div>
                  <div className="audit-sub">{row.client_label ?? ''}</div>
                </td>
                <td>
                  <div className="audit-action">{row.action_label}</div>
                  <div className="audit-sub audit-ellipsis" title={row.summary}>
                    {row.summary}
                  </div>
                </td>
                <td>
                  <div className="mono">{row.ibkr_account ?? '—'}</div>
                  <div className="audit-sub audit-ellipsis" title={row.target_id ?? ''}>
                    {row.target_type ? `${row.target_type}: ${row.target_id ?? '—'}` : ''}
                  </div>
                </td>
                <td>
                  <span className={resultClass(row.result)}>{row.result}</span>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
