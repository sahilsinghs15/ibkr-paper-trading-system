import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { auditErrorMessage, fetchAuditEvent } from '../../api/auditApi'
import { EMPTY_AUDIT_FILTERS, type AuditEventDetail, type AuditFilters } from '../../types/audit'
import type { DisplayTimezone } from '../../types/position'
import { fmtTime } from '../../utils/format'
import { actorLabel, displayValue, RESULT_HINT, resultClass } from './auditFormat'

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
  operation_id: { label: 'Kill-switch operation', filter: 'correlation_id' },
  idempotency_key: { label: 'Idempotency key', filter: 'correlation_id' },
  session_id: { label: 'Session ID', filter: 'session_id' },
  con_id: { label: 'Contract ID', filter: 'ref_id' },
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

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function formatAge(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`
  return `${(seconds / 3600).toFixed(1)} h`
}

// ─── Building blocks ─────────────────────────────────────────────────────────

/** Long identifier: middle-truncated, full value on hover, one-click copy. */
function Id({ value, head = 8, tail = 6 }: { value: string | number | null | undefined; head?: number; tail?: number }) {
  const [copied, setCopied] = useState(false)
  if (value === null || value === undefined || value === '') return <span className="ad-muted">—</span>
  const full = String(value)
  const short = full.length > head + tail + 1 ? `${full.slice(0, head)}…${full.slice(-tail)}` : full
  const copy = (e: React.MouseEvent) => {
    e.stopPropagation()
    void navigator.clipboard?.writeText(full).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    })
  }
  return (
    <span className="ad-id" title={full}>
      <span className="mono">{short}</span>
      <button type="button" className="ad-copy" onClick={copy} aria-label={`Copy ${full}`}>
        {copied ? '✓' : '⧉'}
      </button>
    </span>
  )
}

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="ad-section">
      <header className="ad-section-head">
        <h4>{title}</h4>
        {aside}
      </header>
      {children}
    </section>
  )
}

function Rows({ children }: { children: ReactNode }) {
  return <dl className="ad-rows">{children}</dl>
}

function Row({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="ad-row">
      <dt>{label}</dt>
      <dd>
        {children ?? <span className="ad-muted">—</span>}
        {hint && <span className="ad-hint">{hint}</span>}
      </dd>
    </div>
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
  const text = value === true ? 'same as sign-in' : value === false ? 'differs from sign-in' : 'n/a'
  return (
    <span className={`audit-observation ${cls}`}>
      {label}: {text}
    </span>
  )
}

function Disclosure({ summary, children, defaultOpen = false }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  return (
    <details className="ad-disclosure" open={defaultOpen}>
      <summary>{summary}</summary>
      <div className="ad-disclosure-body">{children}</div>
    </details>
  )
}

function Json({ value }: { value: unknown }) {
  return <pre className="ad-json">{JSON.stringify(value, null, 2)}</pre>
}

/** Scalar → inline text; objects/arrays → compact summary with the full value expandable. */
function Value({ v }: { v: unknown }) {
  if (v === null || v === undefined || v === '') return <span className="ad-muted">—</span>
  if (typeof v === 'object') {
    const n = Array.isArray(v) ? v.length : Object.keys(v as object).length
    return (
      <Disclosure summary={<span className="ad-muted">{Array.isArray(v) ? `${n} items` : `${n} fields`}</span>}>
        <Json value={v} />
      </Disclosure>
    )
  }
  const s = displayValue(v)
  return s.length > 48 ? <Id value={s} head={22} tail={10} /> : <span className="mono">{s}</span>
}

// ─── Body ────────────────────────────────────────────────────────────────────

function Body({ ev, displayTz, onPivot, onOpenEvent }: { ev: AuditEventDetail } & Omit<Props, 'eventId' | 'onClose'>) {
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
    ev.completed_at && ev.occurred_at ? new Date(ev.completed_at).getTime() - new Date(ev.occurred_at).getTime() : null
  const params = Object.entries(ev.parameters ?? {})
  const related = Object.entries(ev.related ?? {}).filter(([, v]) => v != null && v !== '')
  const changes = ev.changes ?? []
  const hasState = ev.before_state != null || ev.after_state != null

  return (
    <>
      {ev.provenance !== 'NATIVE' && (
        <div className="ad-note legacy">
          Legacy record imported from <span className="mono">{String(legacy.source_table ?? ev.legacy_ref)}</span>. Only fields
          captured at the time are shown.
        </div>
      )}

      {/* 1. Actor */}
      <Section title="Actor" aside={<span className="ad-muted ad-small">authenticated by the server</span>}>
        <Rows>
          <Row label="User">
            {ev.actor_email ? (
              <PivotLink title="All actions by this user" onClick={() => onPivot(scoped({ actor: ev.actor_email ?? '' }))}>
                {ev.actor_email}
              </PivotLink>
            ) : (
              actorLabel(ev)
            )}
          </Row>
          {ev.actor_user_id != null && <Row label="User ID"><span className="mono">{ev.actor_user_id}</span></Row>}
          <Row label="Role">{ev.actor_role}</Row>
          <Row label="Type" hint={identity.service_label != null ? String(identity.service_label) : undefined}>
            {ev.actor_type}
          </Row>
          <Row label="Authentication">{ev.auth_method}</Row>
          <Row label="Session">
            {ev.session_id ? (
              <span className="ad-inline">
                <Id value={ev.session_id} />
                <PivotLink
                  title="Everything done in this session, oldest first"
                  onClick={() => onPivot(scoped({ session_id: ev.session_id ?? '' }), 'oldest')}
                >
                  timeline
                </PivotLink>
              </span>
            ) : (
              <span className="ad-muted">No server-side session</span>
            )}
          </Row>
          {inv.session && (
            <>
              <Row label="Signed in">{at(inv.session.created_at)}</Row>
              <Row label="Session ended">
                {inv.session.ended_at ? `${at(inv.session.ended_at)} · ${inv.session.end_reason ?? ''}` : 'Active or expired'}
              </Row>
              {inv.session.login_audit_event_id && (
                <Row label="Sign-in record">
                  <PivotLink title="Open the sign-in audit event" onClick={() => onOpenEvent(inv.session!.login_audit_event_id!)}>
                    open sign-in event
                  </PivotLink>
                </Row>
              )}
            </>
          )}
        </Rows>
      </Section>

      {/* 2. Source / Client */}
      <Section title="Source / Client" aside={<span className="ad-muted ad-small">observed, not proof of person</span>}>
        <Rows>
          <Row label="Source IP" hint={network.client_ip_scope ? String(network.client_ip_scope) : undefined}>
            {ev.client_ip ? (
              <PivotLink title="All actions from this IP" onClick={() => onPivot(scoped({ ip: ev.client_ip ?? '' }))}>
                <span className="mono">{ev.client_ip}</span>
              </PivotLink>
            ) : (
              String(network.peer_label ?? '—')
            )}
          </Row>
          <Row label="Browser">
            {client.browser ? (
              <PivotLink title="All actions from this browser" onClick={() => onPivot(scoped({ browser: String(client.browser) }))}>
                {[client.browser, client.browser_version].filter(Boolean).join(' ')}
              </PivotLink>
            ) : null}
          </Row>
          <Row label="OS" hint={client.device_class ? String(client.device_class) : undefined}>
            {[client.os, client.os_version].filter(Boolean).join(' ') || null}
          </Row>
          <Row label="Device / install" hint="client-reported">
            {ev.client_device_id ? (
              <span className="ad-inline">
                <Id value={ev.client_device_id} />
                <PivotLink title="All actions from this browser install" onClick={() => onPivot(scoped({ device_id: ev.client_device_id ?? '' }))}>
                  filter
                </PivotLink>
              </span>
            ) : client.device_id_invalid ? (
              <span className="ad-muted">invalid value discarded</span>
            ) : null}
          </Row>
          <Row label="App version" hint="client-reported">
            {client.app_version ? <span className="mono">{String(client.app_version)}</span> : null}
          </Row>
          {inv.session?.login_ip && <Row label="IP at sign-in"><span className="mono">{inv.session.login_ip}</span></Row>}
        </Rows>
        {Object.keys(observations).length > 0 && (
          <div className="audit-observations">
            <Observation label="IP" value={observations.ip_matches_login} />
            <Observation label="Device" value={observations.device_matches_login} />
            <Observation label="Browser" value={observations.user_agent_matches_login} />
            {typeof observations.session_age_seconds === 'number' && (
              <span className="audit-observation unknown">Session age {formatAge(observations.session_age_seconds)}</span>
            )}
          </div>
        )}
        <Disclosure summary="Network & client details">
          <Rows>
            <Row label="X-Forwarded-For"><Value v={network.forwarded_for_header} /></Row>
            <Row label="Handled by">{[server.process, server.host].filter(Boolean).join(' @ ') || null}</Row>
            <Row label="User-Agent"><span className="ad-wrap mono">{ev.user_agent ?? '—'}</span></Row>
          </Rows>
        </Disclosure>
      </Section>

      {/* 3. Action */}
      <Section title="Action">
        <Rows>
          <Row label="Action" hint={ev.action}>{ev.action_label}</Row>
          <Row label="Category">{ev.category}</Row>
          <Row label="Summary"><span className="ad-wrap">{ev.summary}</span></Row>
          <Row label="Account">
            {ev.ibkr_account ? (
              <PivotLink title="All actions on this account" onClick={() => onPivot(scoped({ account: ev.ibkr_account ?? '' }))}>
                <span className="mono">{ev.ibkr_account}</span>
              </PivotLink>
            ) : ev.account_id != null ? (
              <span className="mono">{ev.account_id}</span>
            ) : null}
          </Row>
          <Row label="Resource" hint={ev.target_type ?? undefined}>
            {ev.target_id ? <Id value={ev.target_id} head={18} tail={8} /> : null}
          </Row>
          <Row label="Time">{at(ev.occurred_at)}</Row>
        </Rows>
      </Section>

      {/* 4. Parameters */}
      <Section title="Parameters" aside={<span className="ad-muted ad-small">{params.length} supplied</span>}>
        {params.length === 0 ? (
          <div className="ad-empty">No parameters.</div>
        ) : (
          <Rows>
            {params.map(([k, v]) => (
              <Row key={k} label={humanize(k)}>
                <Value v={v} />
              </Row>
            ))}
          </Rows>
        )}
      </Section>

      {/* 5. Before / After */}
      <Section title="Before / After">
        {changes.length > 0 ? (
          <table className="ad-changes">
            <thead>
              <tr>
                <th>Field</th>
                <th>Before</th>
                <th>After</th>
              </tr>
            </thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.field}>
                  <td>{humanize(c.field)}</td>
                  <td className="mono ad-before">{displayValue(c.before)}</td>
                  <td className="mono ad-after">{displayValue(c.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="ad-empty">
            {hasState ? 'No field-level difference between the snapshots.' : 'No state snapshot recorded for this action.'}
          </div>
        )}
        {ev.before_state != null && (
          <Disclosure summary="View full state — before">
            <Json value={ev.before_state} />
          </Disclosure>
        )}
        {ev.after_state != null && (
          <Disclosure summary="View full state — after / result details">
            <Json value={ev.after_state} />
          </Disclosure>
        )}
      </Section>

      {/* 6. Result */}
      <Section title="Result">
        <Rows>
          <Row label="Status" hint={RESULT_HINT[ev.result]}>
            <span className={resultClass(ev.result)}>{ev.result}</span>
          </Row>
          {ev.result_reason && (
            <Row label="Reason">
              <span className="ad-wrap">{ev.result_reason}</span>
            </Row>
          )}
          <Row label="Started">{at(ev.occurred_at)}</Row>
          <Row label="Completed" hint={durationMs != null ? `${durationMs} ms` : undefined}>
            {ev.completed_at ? at(ev.completed_at) : null}
          </Row>
        </Rows>
      </Section>

      {/* 7. Related / Correlation */}
      <Section title="Related / Correlation">
        <Rows>
          {related
            .filter(([k]) => ['internal_order_id', 'internal_order_ids', 'trade_id', 'operation_id'].includes(k))
            .map(([key, value]) => {
              const meta = RELATED_LABELS[key]
              return (
                <Row key={key} label={meta.label}>
                  <span className="ad-stack">
                    {(Array.isArray(value) ? value : [value]).map((item) => (
                      <span key={String(item)} className="ad-inline">
                        <Id value={String(item)} head={14} tail={8} />
                        <PivotLink title={`All events with this ${meta.label.toLowerCase()}`} onClick={() => onPivot(scoped({ [meta.filter]: String(item) }), 'oldest')}>
                          filter
                        </PivotLink>
                      </span>
                    ))}
                  </span>
                </Row>
              )
            })}
        </Rows>
        <Disclosure summary={`Technical identifiers (${related.length + 3})`}>
          <Rows>
            <Row label="Correlation / request">
              {ev.request_id ? (
                <span className="ad-inline">
                  <Id value={ev.request_id} />
                  <PivotLink title="All events with this correlation ID" onClick={() => onPivot(scoped({ correlation_id: ev.request_id ?? '' }))}>
                    filter
                  </PivotLink>
                </span>
              ) : null}
            </Row>
            {related.map(([key, value]) => {
              const meta = RELATED_LABELS[key] ?? { label: humanize(key), filter: 'ref_id' as const }
              return (
                <Row key={key} label={meta.label}>
                  <span className="ad-stack">
                    {(Array.isArray(value) ? value : [value]).map((item) => (
                      <span key={String(item)} className="ad-inline">
                        <Id value={String(item)} head={14} tail={8} />
                        <PivotLink title={`All events with this ${meta.label.toLowerCase()}`} onClick={() => onPivot(scoped({ [meta.filter]: String(item) }), 'oldest')}>
                          filter
                        </PivotLink>
                      </span>
                    ))}
                  </span>
                </Row>
              )
            })}
            <Row label="Audit event ID"><Id value={ev.event_id} /></Row>
            <Row label="Endpoint"><span className="mono ad-wrap">{[ev.http_method, ev.http_path].filter(Boolean).join(' ') || '—'}</span></Row>
          </Rows>
        </Disclosure>

        {ev.actor_user_id != null && (
          <Disclosure summary={`Investigation context · ${inv.concurrent_sessions.length} other live session(s)`}>
            <p className="ad-muted ad-small">Observations to help assess credential misuse — not verdicts.</p>
            <Rows>
              <Row label="Events in session">{inv.session_event_count ?? null}</Row>
              <Row label="Device first seen">{inv.device_first_seen_at ? at(inv.device_first_seen_at) : null}</Row>
              <Row label="User IPs ±24h">
                {inv.actor_ips_within_24h.length ? (
                  <span className="ad-stack">
                    {inv.actor_ips_within_24h.map((x) => (
                      <PivotLink key={x.ip} title="All actions from this IP" onClick={() => onPivot(scoped({ ip: x.ip }))}>
                        <span className="mono">{x.ip}</span> <span className="ad-muted">({x.events})</span>
                      </PivotLink>
                    ))}
                  </span>
                ) : null}
              </Row>
            </Rows>
            {inv.concurrent_sessions.length > 0 && (
              <table className="ad-changes">
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Signed in</th>
                    <th>IP</th>
                  </tr>
                </thead>
                <tbody>
                  {inv.concurrent_sessions.map((s) => (
                    <tr key={s.session_id}>
                      <td>
                        <PivotLink title="Show this session's timeline" onClick={() => onPivot(scoped({ session_id: s.session_id }), 'oldest')}>
                          <span className="mono">{s.session_id.slice(0, 8)}…</span>
                        </PivotLink>
                      </td>
                      <td className="mono">{fmtTime(s.created_at, displayTz)}</td>
                      <td className="mono">{s.login_ip ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Disclosure>
        )}

        <Disclosure summary="Raw record (JSON)">
          <Json value={ev} />
        </Disclosure>
      </Section>
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
      <aside className="audit-drawer ad-drawer" role="dialog" aria-modal="true" aria-label="Audit event detail" onClick={(e) => e.stopPropagation()}>
        <header className="ad-head">
          <div className="ad-head-top">
            <span className="ad-eyebrow">Audit event</span>
            <div className="ad-inline">
              <button type="button" className="history-filter-btn" onClick={copy} disabled={!data}>
                {copied ? 'Copied' : 'Copy JSON'}
              </button>
              <button type="button" className="audit-close" onClick={onClose} aria-label="Close">
                ✕
              </button>
            </div>
          </div>
          {data ? (
            <div className={`ad-banner ${resultClass(data.result).replace('audit-result', '').trim() || 'neutral'}`}>
              <div className="ad-banner-main">
                <span className={resultClass(data.result)}>{data.result}</span>
                <h3>{data.action_label}</h3>
              </div>
              <div className="ad-banner-sub">
                {fmtTime(data.occurred_at, displayTz, { withZone: true })}
                {' · '}
                {data.actor_email ?? actorLabel(data)}
                {data.ibkr_account ? ` · ${data.ibkr_account}` : ''}
              </div>
              {data.result_reason && <div className="ad-banner-reason">{data.result_reason}</div>}
            </div>
          ) : (
            <h3 className="ad-loading-title">Audit event</h3>
          )}
        </header>
        <div className="ad-body">
          {isLoading && <div className="ad-empty">Loading…</div>}
          {error && <div className="status-badge off">{auditErrorMessage(error)}</div>}
          {data && <Body ev={data} displayTz={displayTz} onPivot={onPivot} onOpenEvent={onOpenEvent} />}
        </div>
      </aside>
    </div>
  )
}
