import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { auditErrorMessage, fetchAuditEvent } from '../../api/auditApi'
import type { AuditEventDetail, AuditFilters } from '../../types/audit'
import type { DisplayTimezone } from '../../types/position'
import { fmtTime } from '../../utils/format'
import { actorLabel, displayValue, RESULT_HINT, resultClass, shortId } from './auditFormat'

interface Props {
  eventId: string
  displayTz: DisplayTimezone
  onClose: () => void
  onPivot: (patch: Partial<AuditFilters>, sort?: 'newest' | 'oldest') => void
  onOpenEvent: (eventId: string) => void
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="audit-section">
      <h4>{title}</h4>
      {children}
    </section>
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

function Pivot({ onClick, children, title }: { onClick: () => void; children: ReactNode; title: string }) {
  return (
    <button type="button" className="audit-pivot" title={title} onClick={onClick}>
      {children}
    </button>
  )
}

function Observation({ label, value }: { label: string; value: unknown }) {
  const cls = value === true ? 'match' : value === false ? 'differs' : 'unknown'
  const text = value === true ? 'same as login' : value === false ? 'differs from login' : 'not available'
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

function Body({ ev, displayTz, onPivot, onOpenEvent }: { ev: AuditEventDetail } & Omit<Props, 'eventId' | 'onClose'>) {
  const ctx = ev.context ?? {}
  const network = asRecord(ctx.network)
  const client = asRecord(ctx.client)
  const server = asRecord(ctx.server)
  const identity = asRecord(ctx.identity)
  const observations = asRecord(ctx.session_observations)
  const legacy = asRecord(ctx.legacy)
  const inv = ev.investigation
  const durationMs =
    ev.completed_at && ev.occurred_at
      ? new Date(ev.completed_at).getTime() - new Date(ev.occurred_at).getTime()
      : null
  const relatedEntries = Object.entries(ev.related ?? {})

  return (
    <>
      <div className="audit-callout">
        Identity below is the account the server <strong>authenticated</strong> for this request.
        IP, client and device details are <strong>observed technical context</strong>. Neither proves
        which person physically operated the device.
      </div>
      {ev.provenance !== 'NATIVE' && (
        <div className="audit-callout legacy">
          Legacy record imported from <span className="mono">{String(legacy.source_table ?? ev.legacy_ref)}</span>.
          Only fields captured at the time are shown; missing IP/session/role were never recorded.
        </div>
      )}

      <Section title="Who — authenticated identity">
        <dl className="audit-kv">
          <Field label="Actor">{actorLabel(ev)}</Field>
          <Field label="Actor type">{ev.actor_type}</Field>
          {ev.actor_user_id != null && (
            <Field label="User ID">
              <Pivot title="All actions by this user" onClick={() => onPivot({ actor: ev.actor_email ?? '' })}>
                {ev.actor_user_id}
              </Pivot>
            </Field>
          )}
          <Field label="Role (at the time)">{ev.actor_role}</Field>
          <Field label="Auth method">{ev.auth_method}</Field>
          {identity.service_label != null && <Field label="Service">{String(identity.service_label)}</Field>}
          <Field label="Session">
            {ev.session_id ? (
              <Pivot
                title="Show this session's full timeline (oldest first)"
                onClick={() => onPivot({ ...emptyScope(), session_id: ev.session_id ?? '' }, 'oldest')}
              >
                <span className="mono">{shortId(ev.session_id, 13)}</span> · timeline
              </Pivot>
            ) : (
              'No server-side session'
            )}
          </Field>
          {inv.session && (
            <>
              <Field label="Session created">{fmtTime(inv.session.created_at, displayTz, { withZone: true })}</Field>
              <Field label="Session ended">
                {inv.session.ended_at
                  ? `${fmtTime(inv.session.ended_at, displayTz, { withZone: true })} (${inv.session.end_reason ?? ''})`
                  : 'Still active or expired'}
              </Field>
              <Field label="Login IP">
                <span className="mono">{inv.session.login_ip ?? '—'}</span>
              </Field>
              {inv.session.login_audit_event_id && (
                <Field label="Login event">
                  <Pivot title="Open the authentication event" onClick={() => onOpenEvent(inv.session!.login_audit_event_id!)}>
                    open login record
                  </Pivot>
                </Field>
              )}
            </>
          )}
        </dl>
      </Section>

      <Section title="From where — observed request context">
        <dl className="audit-kv">
          <Field label="Client IP">
            {ev.client_ip ? (
              <Pivot title="All actions from this IP" onClick={() => onPivot({ ...emptyScope(), ip: ev.client_ip ?? '' })}>
                <span className="mono">{ev.client_ip}</span>
              </Pivot>
            ) : (
              String(network.peer_label ?? '—')
            )}
          </Field>
          <Field label="Network scope">{String(network.client_ip_scope ?? '—')}</Field>
          {network.forwarded_for_header != null && (
            <Field label="X-Forwarded-For (as received)">
              <span className="mono">{String(network.forwarded_for_header)}</span>
            </Field>
          )}
          <Field label="Browser">
            {[client.browser, client.browser_version].filter(Boolean).join(' ') || '—'}
          </Field>
          <Field label="OS / device class">
            {[client.os, client.os_version].filter(Boolean).join(' ') || '—'}
            {client.device_class ? ` · ${String(client.device_class)}` : ''}
          </Field>
          <Field label="App version (client-reported)">{String(client.app_version ?? '—')}</Field>
          <Field label="Device ID (client-reported)">
            {ev.client_device_id ? (
              <Pivot title="All actions from this browser install" onClick={() => onPivot({ ...emptyScope(), device_id: ev.client_device_id ?? '' })}>
                <span className="mono">{shortId(ev.client_device_id, 13)}</span>
              </Pivot>
            ) : client.device_id_invalid ? (
              'Invalid value supplied (discarded)'
            ) : (
              '—'
            )}
          </Field>
          <Field label="Handled by">
            {[server.process, server.host].filter(Boolean).join(' @ ') || '—'}
          </Field>
        </dl>
        {Object.keys(observations).length > 0 && (
          <div className="audit-observations">
            <Observation label="IP" value={observations.ip_matches_login} />
            <Observation label="Device" value={observations.device_matches_login} />
            <Observation label="Browser" value={observations.user_agent_matches_login} />
            {typeof observations.session_age_seconds === 'number' && (
              <span className="audit-observation unknown">
                Session age: {formatAge(observations.session_age_seconds)}
              </span>
            )}
          </div>
        )}
        {ev.user_agent && (
          <details className="audit-json">
            <summary>User-Agent</summary>
            <pre>{ev.user_agent}</pre>
          </details>
        )}
      </Section>

      <Section title="What — action">
        <dl className="audit-kv">
          <Field label="Category">{ev.category}</Field>
          <Field label="Action">
            {ev.action_label} <span className="mono audit-muted">({ev.action})</span>
          </Field>
          <Field label="Summary">{ev.summary}</Field>
          <Field label="Account">
            {ev.ibkr_account ? (
              <Pivot title="All actions on this account" onClick={() => onPivot({ ...emptyScope(), account: ev.ibkr_account ?? '' })}>
                <span className="mono">{ev.ibkr_account}</span>
              </Pivot>
            ) : ev.account_id != null ? (
              String(ev.account_id)
            ) : (
              '—'
            )}
          </Field>
          <Field label="Target">
            {ev.target_type ? (
              <span className="mono audit-break">
                {ev.target_type}: {ev.target_id ?? '—'}
              </span>
            ) : (
              '—'
            )}
          </Field>
          <Field label="Endpoint">
            <span className="mono">{[ev.http_method, ev.http_path].filter(Boolean).join(' ') || '—'}</span>
          </Field>
        </dl>
        <h5>Parameters</h5>
        <KeyValues data={ev.parameters ?? {}} />
      </Section>

      <Section title="Change — before / after">
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
        <JsonBlock label="Before state (full)" value={ev.before_state} />
        <JsonBlock label="After state / result details (full)" value={ev.after_state} />
      </Section>

      <Section title="Result">
        <dl className="audit-kv">
          <Field label="Result">
            <span className={resultClass(ev.result)}>{ev.result}</span>{' '}
            <span className="audit-muted">{RESULT_HINT[ev.result] ?? ''}</span>
          </Field>
          <Field label="Reason">{ev.result_reason}</Field>
          <Field label="Occurred">{fmtTime(ev.occurred_at, displayTz, { withZone: true })}</Field>
          <Field label="Completed">
            {ev.completed_at
              ? `${fmtTime(ev.completed_at, displayTz, { withZone: true })}${durationMs != null ? ` (${durationMs} ms)` : ''}`
              : '—'}
          </Field>
        </dl>
      </Section>

      <Section title="Correlation">
        <dl className="audit-kv">
          <Field label="Event ID">
            <span className="mono audit-break">{ev.event_id}</span>
          </Field>
          <Field label="Request ID">
            <span className="mono">{ev.request_id ?? '—'}</span>
          </Field>
          {relatedEntries.map(([k, v]) => (
            <Field key={k} label={k}>
              {(Array.isArray(v) ? v : [v]).map((item) =>
                item == null ? null : (
                  <Pivot
                    key={String(item)}
                    title="Find every audit event referencing this identifier"
                    onClick={() => onPivot({ ...emptyScope(), ref_id: String(item) }, 'oldest')}
                  >
                    <span className="mono">{String(item)}</span>
                  </Pivot>
                ),
              )}
            </Field>
          ))}
        </dl>
      </Section>

      {ev.actor_user_id != null && (
        <Section title="Investigation context (observations, not verdicts)">
          <dl className="audit-kv">
            <Field label="Events in this session">{inv.session_event_count ?? '—'}</Field>
            <Field label="Device first seen for user">
              {inv.device_first_seen_at ? fmtTime(inv.device_first_seen_at, displayTz, { withZone: true }) : '—'}
            </Field>
            <Field label="IPs used by user ±24h">
              {inv.actor_ips_within_24h.length
                ? inv.actor_ips_within_24h.map((x) => (
                    <Pivot key={x.ip} title="All actions from this IP" onClick={() => onPivot({ ...emptyScope(), ip: x.ip })}>
                      <span className="mono">
                        {x.ip} ({x.events})
                      </span>
                    </Pivot>
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
                  <th>Created</th>
                  <th>Login IP</th>
                  <th>Device</th>
                </tr>
              </thead>
              <tbody>
                {inv.concurrent_sessions.map((s) => (
                  <tr key={s.session_id}>
                    <td>
                      <Pivot title="Show this session's timeline" onClick={() => onPivot({ ...emptyScope(), session_id: s.session_id }, 'oldest')}>
                        <span className="mono">{shortId(s.session_id, 13)}</span>
                      </Pivot>
                    </td>
                    <td className="mono">{fmtTime(s.created_at, displayTz)}</td>
                    <td className="mono">{s.login_ip ?? '—'}</td>
                    <td className="mono">{shortId(s.login_device_id, 13)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>
      )}

      <JsonBlock label="Raw record" value={ev} />
    </>
  )
}

/** Pivots replace the investigation scope rather than stacking onto it. */
function emptyScope(): Partial<AuditFilters> {
  return {
    actor: '',
    ip: '',
    account: '',
    session_id: '',
    device_id: '',
    ref_id: '',
    categories: [],
    action: '',
    result: '',
    q: '',
    role: '',
    date_from: '',
    date_to: '',
  }
}

function formatAge(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`
  return `${(seconds / 3600).toFixed(1)} h`
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
