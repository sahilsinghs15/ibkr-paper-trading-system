import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { auditErrorMessage, fetchAuditEvent } from '../../api/auditApi'
import { EMPTY_AUDIT_FILTERS, type AuditEventDetail, type AuditFilters } from '../../types/audit'
import type { DisplayTimezone } from '../../types/position'
import { fmtTime } from '../../utils/format'
import { actorLabel, displayValue, RESULT_HINT, resultClass, shortId } from './auditFormat'

type Pivot = (patch: Partial<AuditFilters>, sort?: 'newest' | 'oldest') => void

interface Props {
  eventId: string
  displayTz: DisplayTimezone
  onClose: () => void
  onPivot: Pivot
  onOpenEvent: (eventId: string) => void
}

/** Pivots replace the investigation scope rather than stacking onto it. */
function scoped(patch: Partial<AuditFilters>): Partial<AuditFilters> {
  return { ...EMPTY_AUDIT_FILTERS, ...patch }
}

type RefFilter = 'order_id' | 'trade_id' | 'correlation_id' | 'session_id' | 'ref_id'

/** Explicit names for identifiers recorded in `related`, and the filter each pivots to. */
const RELATED_LABELS: Record<string, { label: string; filter: RefFilter }> = {
  internal_order_id: { label: 'Order ID', filter: 'order_id' },
  internal_order_ids: { label: 'Order IDs', filter: 'order_id' },
  broker_order_id: { label: 'Broker order ID', filter: 'order_id' },
  perm_id: { label: 'IBKR perm ID', filter: 'order_id' },
  manual_order_id: { label: 'Manual order record', filter: 'order_id' },
  trade_id: { label: 'Trade / position ID', filter: 'trade_id' },
  operation_id: { label: 'Kill-switch operation ID', filter: 'correlation_id' },
  idempotency_key: { label: 'Idempotency key', filter: 'correlation_id' },
  session_id: { label: 'Session ID', filter: 'session_id' },
  con_id: { label: 'Contract ID (conId)', filter: 'ref_id' },
  symbol: { label: 'Symbol', filter: 'ref_id' },
  symbols: { label: 'Symbols', filter: 'ref_id' },
  allocation_id: { label: 'Allocation ID', filter: 'ref_id' },
  service: { label: 'Service', filter: 'ref_id' },
  unit: { label: 'Systemd unit', filter: 'ref_id' },
  legacy_event_log_id: { label: 'Legacy event_log row', filter: 'ref_id' },
  legacy_manual_audit_event_id: { label: 'Legacy manual audit row', filter: 'ref_id' },
}

function humanize(key: string): string {
  const s = key.replace(/_/g, ' ')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function Band({ tone, title, note, children }: { tone: string; title: string; note?: ReactNode; children: ReactNode }) {
  return (
    <section className={`audit-band ${tone}`}>
      <h4>{title}</h4>
      {note && <p className="audit-band-note">{note}</p>}
      {children}
    </section>
  )
}

function Sub({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="audit-sub-section">
      <h5>{title}</h5>
      {children}
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children ?? '—'}</dd>
    </>
  )
}

function PivotLink({ onClick, children, title }: { onClick: () => void; children: ReactNode; title: string }) {
  return (
    <button type="button" className="audit-pivot" title={title} onClick={onClick}>
      {children}
    </button>
  )
}

function Observation({ label, value }: { label: string; value: unknown }) {
  const cls = value === true ? 'match' : value === false ? 'differs' : 'unknown'
  const text = value === true ? 'same as at login' : value === false ? 'differs from login' : 'not available'
  return (
    <span className={`audit-observation ${cls}`}>
      {label}: {text}
    </span>
  )
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function KeyValues({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data)
  if (!entries.length) return <div className="audit-muted">None</div>
  return (
    <dl className="audit-kv">
      {entries.map(([k, v]) => (
        <Field key={k} label={k}>
          <span className="mono audit-break">{displayValue(v)}</span>
        </Field>
      ))}
    </dl>
  )
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  if (value === null || value === undefined) return null
  return (
    <details className="audit-json">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  )
}

function formatAge(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`
  return `${(seconds / 3600).toFixed(1)} h`
}

function Body({
  ev,
  displayTz,
  onPivot,
  onOpenEvent,
}: { ev: AuditEventDetail } & Omit<Props, 'eventId' | 'onClose'>) {
  const ctx = ev.context ?? {}
  const network = asRecord(ctx.network)
  const client = asRecord(ctx.client)
  const server = asRecord(ctx.server)
  const identity = asRecord(ctx.identity)
  const observations = asRecord(ctx.session_observations)
  const legacy = asRecord(ctx.legacy)
  const inv = ev.investigation
  const at = (iso: string | null | undefined) => fmtTime(iso, displayTz, { withZone: true })
  const durationMs =
    ev.completed_at && ev.occurred_at
      ? new Date(ev.completed_at).getTime() - new Date(ev.occurred_at).getTime()
      : null
  const relatedEntries = Object.entries(ev.related ?? {}).filter(([, v]) => v != null && v !== '')

  return (
    <>
      {ev.provenance !== 'NATIVE' && (
        <div className="audit-callout legacy">
          Legacy record imported from <span className="mono">{String(legacy.source_table ?? ev.legacy_ref)}</span>.
          Only fields captured at the time are shown; missing IP, session and role were never recorded.
        </div>
      )}

      {/* ---------------------------------------------------------- 1 */}
      <Band
        tone="actor"
        title="Authenticated actor"
        note="The account the server authenticated for this request (from the signed session token)."
      >
        <dl className="audit-kv">
          <Field label="User">{actorLabel(ev)}</Field>
          <Field label="User ID">
            {ev.actor_user_id != null ? (
              <PivotLink title="All actions by this user" onClick={() => onPivot(scoped({ actor: ev.actor_email ?? '' }))}>
                {ev.actor_user_id}
              </PivotLink>
            ) : (
              '—'
            )}
          </Field>
          <Field label="Role at the time">{ev.actor_role}</Field>
          <Field label="Actor type">{ev.actor_type}</Field>
          {identity.service_label != null && <Field label="Service caller">{String(identity.service_label)}</Field>}
          <Field label="Authentication">{ev.auth_method}</Field>
          <Field label="Session">
            {ev.session_id ? (
              <PivotLink
                title="Show everything done in this session, oldest first"
                onClick={() => onPivot(scoped({ session_id: ev.session_id ?? '' }), 'oldest')}
              >
                <span className="mono">{shortId(ev.session_id, 13)}</span> · session timeline
              </PivotLink>
            ) : (
              'No server-side session'
            )}
          </Field>
          {inv.session && (
            <>
              <Field label="Signed in">{at(inv.session.created_at)}</Field>
              <Field label="Session ended">
                {inv.session.ended_at
                  ? `${at(inv.session.ended_at)} (${inv.session.end_reason ?? ''})`
                  : 'Still active or expired'}
              </Field>
              {inv.session.login_audit_event_id && (
                <Field label="Sign-in record">
                  <PivotLink title="Open the sign-in audit event" onClick={() => onOpenEvent(inv.session!.login_audit_event_id!)}>
                    open sign-in event
                  </PivotLink>
                </Field>
              )}
            </>
          )}
        </dl>
      </Band>

      {/* ---------------------------------------------------------- 2 */}
      <Band
        tone="source"
        title="Observed source / client context"
        note={
          <>
            Technical context observed by the server. It supports investigation but does <strong>not</strong> prove
            which person physically operated the device. Client-reported values are unverified.
          </>
        }
      >
        <dl className="audit-kv">
          <Field label="Source IP">
            {ev.client_ip ? (
              <PivotLink title="All actions from this IP" onClick={() => onPivot(scoped({ ip: ev.client_ip ?? '' }))}>
                <span className="mono">{ev.client_ip}</span>
              </PivotLink>
            ) : (
              String(network.peer_label ?? '—')
            )}
            {network.client_ip_scope ? <span className="audit-muted"> · {String(network.client_ip_scope)}</span> : null}
          </Field>
          <Field label="Browser">
            {client.browser ? (
              <PivotLink title="All actions from this browser" onClick={() => onPivot(scoped({ browser: String(client.browser) }))}>
                {[client.browser, client.browser_version].filter(Boolean).join(' ')}
              </PivotLink>
            ) : (
              '—'
            )}
          </Field>
          <Field label="Operating system">
            {[client.os, client.os_version].filter(Boolean).join(' ') || '—'}
            {client.device_class ? <span className="audit-muted"> · {String(client.device_class)}</span> : null}
          </Field>
          <Field label="Device / install ID">
            {ev.client_device_id ? (
              <PivotLink title="All actions from this browser install" onClick={() => onPivot(scoped({ device_id: ev.client_device_id ?? '' }))}>
                <span className="mono">{shortId(ev.client_device_id, 13)}</span>
              </PivotLink>
            ) : client.device_id_invalid ? (
              'Invalid value supplied (discarded)'
            ) : (
              '—'
            )}
            <span className="audit-muted"> · client-reported</span>
          </Field>
          <Field label="App version">
            {String(client.app_version ?? '—')}
            <span className="audit-muted"> · client-reported</span>
          </Field>
          {inv.session?.login_ip && <Field label="IP at sign-in">{<span className="mono">{inv.session.login_ip}</span>}</Field>}
        </dl>
        {Object.keys(observations).length > 0 && (
          <div className="audit-observations">
            <Observation label="IP" value={observations.ip_matches_login} />
            <Observation label="Device" value={observations.device_matches_login} />
            <Observation label="Browser" value={observations.user_agent_matches_login} />
            {typeof observations.session_age_seconds === 'number' && (
              <span className="audit-observation unknown">Session age: {formatAge(observations.session_age_seconds)}</span>
            )}
          </div>
        )}
        <details className="audit-json">
          <summary>Network &amp; server details</summary>
          <dl className="audit-kv">
            <Field label="X-Forwarded-For (as received)">
              <span className="mono">{String(network.forwarded_for_header ?? '—')}</span>
            </Field>
            <Field label="IP source">{String(network.ip_source ?? '—')}</Field>
            <Field label="Handled by">{[server.process, server.host].filter(Boolean).join(' @ ') || '—'}</Field>
            <Field label="User-Agent">
              <span className="mono audit-break">{ev.user_agent ?? '—'}</span>
            </Field>
          </dl>
        </details>
      </Band>

      {/* ---------------------------------------------------------- 3 */}
      <Band tone="action" title="Action / result">
        <dl className="audit-kv">
          <Field label="Time">{at(ev.occurred_at)}</Field>
          <Field label="Category">{ev.category}</Field>
          <Field label="Action">
            {ev.action_label} <span className="mono audit-muted">({ev.action})</span>
          </Field>
          <Field label="Summary">{ev.summary}</Field>
          <Field label="Account">
            {ev.ibkr_account ? (
              <PivotLink title="All actions on this account" onClick={() => onPivot(scoped({ account: ev.ibkr_account ?? '' }))}>
                <span className="mono">{ev.ibkr_account}</span>
              </PivotLink>
            ) : ev.account_id != null ? (
              String(ev.account_id)
            ) : (
              '—'
            )}
          </Field>
          <Field label="Resource">
            {ev.target_type ? (
              <span className="mono audit-break">
                {ev.target_type}: {ev.target_id ?? '—'}
              </span>
            ) : (
              '—'
            )}
          </Field>
          <Field label="Result">
            <span className={resultClass(ev.result)}>{ev.result}</span>{' '}
            <span className="audit-muted">{RESULT_HINT[ev.result] ?? ''}</span>
          </Field>
          <Field label="Reason">{ev.result_reason}</Field>
          <Field label="Completed">
            {ev.completed_at ? `${at(ev.completed_at)}${durationMs != null ? ` (${durationMs} ms)` : ''}` : '—'}
          </Field>
        </dl>

        <Sub title="Parameters">
          <KeyValues data={ev.parameters ?? {}} />
        </Sub>

        <Sub title="Before / after">
          {ev.changes && ev.changes.length > 0 ? (
            <table className="audit-changes">
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Before</th>
                  <th>After</th>
                </tr>
              </thead>
              <tbody>
                {ev.changes.map((c) => (
                  <tr key={c.field}>
                    <td className="mono">{c.field}</td>
                    <td className="mono audit-before">{displayValue(c.before)}</td>
                    <td className="mono audit-after">{displayValue(c.after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="audit-muted">
              {ev.before_state == null && ev.after_state == null
                ? 'No state snapshot for this action.'
                : 'No field-level difference between the recorded snapshots.'}
            </div>
          )}
          <JsonBlock label="Full before state" value={ev.before_state} />
          <JsonBlock label="Full after state / result details" value={ev.after_state} />
        </Sub>
      </Band>

      {/* ---------------------------------------------------------- IDs */}
      <section className="audit-section">
        <h4>Related IDs &amp; correlation</h4>
        <dl className="audit-kv">
          <Field label="Correlation / request ID">
            {ev.request_id ? (
              <PivotLink title="All events with this correlation ID" onClick={() => onPivot(scoped({ correlation_id: ev.request_id ?? '' }))}>
                <span className="mono">{ev.request_id}</span>
              </PivotLink>
            ) : (
              '—'
            )}
          </Field>
          {relatedEntries.map(([key, value]) => {
            const meta = RELATED_LABELS[key] ?? { label: humanize(key), filter: 'ref_id' as const }
            return (
              <Field key={key} label={meta.label}>
                {(Array.isArray(value) ? value : [value]).map((item) =>
                  item == null ? null : (
                    <PivotLink
                      key={String(item)}
                      title={`Find every audit event with this ${meta.label.toLowerCase()}`}
                      onClick={() => onPivot(scoped({ [meta.filter]: String(item) }), 'oldest')}
                    >
                      <span className="mono">{String(item)}</span>
                    </PivotLink>
                  ),
                )}
              </Field>
            )
          })}
          <Field label="Audit event ID">
            <span className="mono audit-break">{ev.event_id}</span>
          </Field>
          <Field label="Endpoint">
            <span className="mono">{[ev.http_method, ev.http_path].filter(Boolean).join(' ') || '—'}</span>
          </Field>
        </dl>
      </section>

      {ev.actor_user_id != null && (
        <section className="audit-section">
          <h4>Investigation context</h4>
          <p className="audit-band-note">Observations to help assess possible credential misuse — not verdicts.</p>
          <dl className="audit-kv">
            <Field label="Events in this session">{inv.session_event_count ?? '—'}</Field>
            <Field label="Device first seen for user">{inv.device_first_seen_at ? at(inv.device_first_seen_at) : '—'}</Field>
            <Field label="User's IPs within ±24h">
              {inv.actor_ips_within_24h.length
                ? inv.actor_ips_within_24h.map((x) => (
                    <PivotLink key={x.ip} title="All actions from this IP" onClick={() => onPivot(scoped({ ip: x.ip }))}>
                      <span className="mono">
                        {x.ip} ({x.events})
                      </span>
                    </PivotLink>
                  ))
                : '—'}
            </Field>
          </dl>
          <h5>Other sessions of this user live at that moment ({inv.concurrent_sessions.length})</h5>
          {inv.concurrent_sessions.length === 0 ? (
            <div className="audit-muted">None.</div>
          ) : (
            <table className="audit-changes">
              <thead>
                <tr>
                  <th>Session</th>
                  <th>Signed in</th>
                  <th>IP at sign-in</th>
                  <th>Device</th>
                </tr>
              </thead>
              <tbody>
                {inv.concurrent_sessions.map((s) => (
                  <tr key={s.session_id}>
                    <td>
                      <PivotLink title="Show this session's timeline" onClick={() => onPivot(scoped({ session_id: s.session_id }), 'oldest')}>
                        <span className="mono">{shortId(s.session_id, 13)}</span>
                      </PivotLink>
                    </td>
                    <td className="mono">{fmtTime(s.created_at, displayTz)}</td>
                    <td className="mono">{s.login_ip ?? '—'}</td>
                    <td className="mono">{shortId(s.login_device_id, 13)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <JsonBlock label="Raw record (JSON)" value={ev} />
    </>
  )
}

export function AuditEventDrawer({ eventId, displayTz, onClose, onPivot, onOpenEvent }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['audit-event', eventId],
    queryFn: () => fetchAuditEvent(eventId),
    staleTime: 5_000,
  })
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const copy = () => {
    if (!data) return
    void navigator.clipboard?.writeText(JSON.stringify(data, null, 2)).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <div className="audit-drawer-backdrop" onClick={onClose} role="presentation">
      <aside
        className="audit-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Audit event detail"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="audit-drawer-head">
          <div>
            <h3>{data?.action_label ?? 'Audit event'}</h3>
            {data && (
              <div className="audit-sub">
                <span className={resultClass(data.result)}>{data.result}</span>{' '}
                {fmtTime(data.occurred_at, displayTz, { withZone: true })} · {data.category}
                {data.actor_email ? ` · ${data.actor_email}` : ''}
              </div>
            )}
          </div>
          <div className="audit-inline">
            <button type="button" className="history-filter-btn" onClick={copy} disabled={!data}>
              {copied ? 'Copied' : 'Copy JSON'}
            </button>
            <button type="button" className="audit-close" onClick={onClose} aria-label="Close">
              ✕
            </button>
          </div>
        </header>
        <div className="audit-drawer-body">
          {isLoading && <div className="audit-muted">Loading…</div>}
          {error && <div className="status-badge off">{auditErrorMessage(error)}</div>}
          {data && <Body ev={data} displayTz={displayTz} onPivot={onPivot} onOpenEvent={onOpenEvent} />}
        </div>
      </aside>
    </div>
  )
}
