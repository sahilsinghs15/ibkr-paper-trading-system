import { useMemo, useState, type ReactNode } from 'react'
import {
  CORRELATION_FILTER_KEYS,
  SECURITY_FILTER_KEYS,
  type AuditFacets,
  type AuditFilters,
} from '../../types/audit'
import { isoToZonedInput } from '../../utils/zonedTime'

interface Props {
  applied: AuditFilters
  facets: AuditFacets | undefined
  activeAccount: string | null
  displayTz: string
  tzLabel: string
  onApply: (filters: AuditFilters) => void
  onClear: () => void
}

const QUICK_RANGES: { label: string; ms: number }[] = [
  { label: 'Last hour', ms: 3_600_000 },
  { label: 'Last 24h', ms: 86_400_000 },
  { label: 'Last 7 days', ms: 7 * 86_400_000 },
  { label: 'Last 30 days', ms: 30 * 86_400_000 },
]

// PENDING and UNKNOWN are outcomes an event normally passes through or never
// reaches, not states it rests in: the recorder commits PENDING as intent and
// finalizes it in the same operation, so a finalized row is only PENDING if the
// process died mid-action. Selecting either usually returns nothing, which reads
// as a broken filter unless the option says so.
function resultOptionLabel(result: string): string {
  const title = result.charAt(0) + result.slice(1).toLowerCase()
  if (result === 'PENDING') return `${title} (interrupted mid-action)`
  if (result === 'UNKNOWN') return `${title} (outcome not recorded)`
  return title
}

// Everything that is not a success, for the "failures" shortcut. Asking "did
// anything fail in this window?" previously meant four separate searches.
const NON_SUCCESS_RESULTS = ['PARTIAL', 'REJECTED', 'DENIED', 'FAILED'] as const

function activeCount(f: AuditFilters, keys: readonly (keyof AuditFilters)[]): number {
  return keys.filter((k) => String(f[k] ?? '').trim() !== '').length
}

function Disclosure({
  title,
  hint,
  count,
  open,
  onToggle,
  children,
}: {
  title: string
  hint: string
  count: number
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <div className={`audit-disclosure ${open ? 'open' : ''}`}>
      <button type="button" className="audit-disclosure-head" aria-expanded={open} onClick={onToggle}>
        <span className="audit-disclosure-caret">{open ? '▾' : '▸'}</span>
        <span className="audit-disclosure-title">{title}</span>
        {count > 0 && <span className="audit-count">{count} active</span>}
        <span className="audit-disclosure-hint">{hint}</span>
      </button>
      {open && <div className="audit-filter-row audit-disclosure-body">{children}</div>}
    </div>
  )
}

/**
 * Three tiers of search:
 *  1. Primary — what an operator normally asks (when, who, what, where, outcome).
 *  2. Advanced / Security — source IP, session, device, browser, role.
 *  3. Technical correlation — order / trade / position / correlation ids.
 * Edits are drafted and applied together as one server-side search.
 */
export function AuditFiltersPanel({
  applied,
  facets,
  activeAccount,
  displayTz,
  tzLabel,
  onApply,
  onClear,
}: Props) {
  const [draft, setDraft] = useState<AuditFilters>(applied)
  const [syncedFrom, setSyncedFrom] = useState<AuditFilters>(applied)
  const [showSecurity, setShowSecurity] = useState(activeCount(applied, SECURITY_FILTER_KEYS) > 0)
  const [showCorrelation, setShowCorrelation] = useState(
    activeCount(applied, CORRELATION_FILTER_KEYS) > 0 || applied.provenance !== 'all',
  )

  // Pivots from the detail drawer change `applied`; mirror them into the draft
  // and reveal the section that now holds an active filter.
  if (syncedFrom !== applied) {
    setSyncedFrom(applied)
    setDraft(applied)
    if (activeCount(applied, SECURITY_FILTER_KEYS) > 0) setShowSecurity(true)
    if (activeCount(applied, CORRELATION_FILTER_KEYS) > 0) setShowCorrelation(true)
  }

  const category = draft.categories[0] ?? ''
  const actionOptions = useMemo(() => {
    const cats = facets?.categories ?? []
    return category ? cats.filter((c) => c.category === category) : cats
  }, [facets, category])

  const set = <K extends keyof AuditFilters>(key: K, value: AuditFilters[K]) =>
    setDraft((d) => ({ ...d, [key]: value }))

  /** Actions still selectable once `category` narrows the list. */
  const actionsWithin = (category: string): Set<string> => {
    const cats = facets?.categories ?? []
    const scoped = category ? cats.filter((c) => c.category === category) : cats
    return new Set(scoped.flatMap((c) => c.actions.map((a) => a.action)))
  }

  const setCategory = (next: string) =>
    setDraft((d) => {
      // Drop only the actions the new category no longer offers. The
      // single-select version cleared the whole choice; with a multi-select
      // that would silently discard a set the operator had just built up.
      const allowed = actionsWithin(next)
      return {
        ...d,
        categories: next ? [next] : [],
        actions: d.actions.filter((a) => allowed.has(a)),
      }
    })

  const toggleIn = (key: 'actions' | 'results', value: string) =>
    setDraft((d) => {
      const current = d[key]
      return {
        ...d,
        [key]: current.includes(value)
          ? current.filter((v) => v !== value)
          : [...current, value],
      }
    })

  const toggleAction = (action: string) => toggleIn('actions', action)
  const toggleResult = (result: string) => toggleIn('results', result)

  /** Select a whole category's actions, or clear them if all are already on. */
  const toggleGroup = (actions: string[]) =>
    setDraft((d) => {
      const allOn = actions.every((a) => d.actions.includes(a))
      if (allOn) {
        return { ...d, actions: d.actions.filter((a) => !actions.includes(a)) }
      }
      const merged = new Set(d.actions)
      actions.forEach((a) => merged.add(a))
      return { ...d, actions: [...merged] }
    })

  const quickRange = (ms: number) =>
    setDraft((d) => ({
      ...d,
      date_from: isoToZonedInput(new Date(Date.now() - ms), displayTz),
      date_to: '',
    }))

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    onApply(draft)
  }

  return (
    <form className="audit-filters" onSubmit={submit}>
      {/* 1. Primary operator investigation */}
      <div className="audit-filter-row">
        <label className="audit-field">
          <span>From</span>
          <input
            type="datetime-local"
            value={draft.date_from}
            onChange={(e) => set('date_from', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>To</span>
          <input type="datetime-local" value={draft.date_to} onChange={(e) => set('date_to', e.target.value)} />
        </label>
        <div className="audit-quick" aria-label="Quick time ranges">
          {QUICK_RANGES.map((r) => (
            <button key={r.label} type="button" className="history-filter-btn" onClick={() => quickRange(r.ms)}>
              {r.label}
            </button>
          ))}
          <span className="audit-tz" title="Dates are entered and shown in the dashboard display timezone (switch in the header)">
            {tzLabel}
          </span>
        </div>
      </div>

      <div className="audit-filter-row">
        <label className="audit-field grow">
          <span>User</span>
          <input
            list="audit-actor-options"
            placeholder="E-mail or part of it"
            value={draft.actor}
            maxLength={255}
            onChange={(e) => set('actor', e.target.value)}
          />
          <datalist id="audit-actor-options">
            {(facets?.actors ?? []).map((a) => (
              <option key={a} value={a} />
            ))}
          </datalist>
        </label>
        <label className="audit-field">
          <span>Category</span>
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {(facets?.categories ?? []).map((c) => (
              <option key={c.category} value={c.category}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <div className="audit-field grow">
          <span>
            Action
            {draft.actions.length > 0 && (
              <button type="button" className="audit-chip-clear" onClick={() => set('actions', [])}>
                clear {draft.actions.length}
              </button>
            )}
          </span>
          {/* Grouped checkboxes rather than a multi-select: 37 actions in a
              ctrl-click list is worse than the single-select it replaces. */}
          <div className="audit-checkgroup audit-checkgroup-scroll">
            {actionOptions.map((c) => (
              <fieldset key={c.category} className="audit-checkgroup-section">
                <legend>
                  <button
                    type="button"
                    className="audit-chip-clear"
                    title={`Select every action in ${c.label}`}
                    onClick={() => toggleGroup(c.actions.map((a) => a.action))}
                  >
                    {c.label}
                  </button>
                </legend>
                {c.actions.map((a) => (
                  <label key={a.action} className="audit-check">
                    <input
                      type="checkbox"
                      checked={draft.actions.includes(a.action)}
                      onChange={() => toggleAction(a.action)}
                    />
                    <span>{a.label}</span>
                  </label>
                ))}
              </fieldset>
            ))}
          </div>
        </div>
        <label className="audit-field">
          <span>Account</span>
          <div className="audit-inline">
            <input
              placeholder="e.g. DU1234567"
              value={draft.account}
              maxLength={64}
              onChange={(e) => set('account', e.target.value)}
            />
            {activeAccount && (
              <button
                type="button"
                className="history-filter-btn"
                title="Use the account currently selected in the dashboard"
                onClick={() => set('account', activeAccount)}
              >
                Current
              </button>
            )}
          </div>
        </label>
        <div className="audit-field">
          <span>
            Result
            {draft.results.length > 0 && (
              <button type="button" className="audit-chip-clear" onClick={() => set('results', [])}>
                clear {draft.results.length}
              </button>
            )}
            <button
              type="button"
              className="audit-chip-clear"
              title="Everything that did not succeed"
              onClick={() => set('results', [...NON_SUCCESS_RESULTS])}
            >
              failures
            </button>
          </span>
          <div className="audit-checkgroup audit-checkgroup-inline">
            {(facets?.results ?? []).map((r) => (
              <label key={r} className="audit-check">
                <input
                  type="checkbox"
                  checked={draft.results.includes(r)}
                  onChange={() => toggleResult(r)}
                />
                <span>{resultOptionLabel(r)}</span>
              </label>
            ))}
          </div>
        </div>
        <label className="audit-field grow">
          <span>Keyword</span>
          <input
            type="search"
            placeholder="Symbol, summary, reason…"
            value={draft.q}
            maxLength={200}
            onChange={(e) => set('q', e.target.value)}
          />
        </label>
      </div>

      {/* 2. Advanced security / actor investigation */}
      <Disclosure
        title="Advanced / Security filters"
        hint="Source IP, session, device, browser, role"
        count={activeCount(draft, SECURITY_FILTER_KEYS)}
        open={showSecurity}
        onToggle={() => setShowSecurity((v) => !v)}
      >
        <label className="audit-field">
          <span>Source IP or range</span>
          <input
            placeholder="203.0.113.7 or 10.0.0.0/8"
            value={draft.ip}
            maxLength={64}
            onChange={(e) => set('ip', e.target.value)}
          />
        </label>
        <label className="audit-field grow">
          <span>Session</span>
          <input
            placeholder="Session ID"
            value={draft.session_id}
            onChange={(e) => set('session_id', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>Device / browser install</span>
          <input
            placeholder="Device ID"
            value={draft.device_id}
            maxLength={64}
            onChange={(e) => set('device_id', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>Browser / client</span>
          <select value={draft.browser} onChange={(e) => set('browser', e.target.value)}>
            <option value="">Any</option>
            {(facets?.browsers ?? []).map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
        </label>
        <label className="audit-field">
          <span>Role</span>
          <select value={draft.role} onChange={(e) => set('role', e.target.value)}>
            <option value="">Any</option>
            {(facets?.roles ?? []).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
      </Disclosure>

      {/* 3. Technical correlation */}
      <Disclosure
        title="Technical correlation"
        hint="Order, trade, position and correlation IDs"
        count={activeCount(draft, CORRELATION_FILTER_KEYS) + (draft.provenance !== 'all' ? 1 : 0)}
        open={showCorrelation}
        onToggle={() => setShowCorrelation((v) => !v)}
      >
        <label className="audit-field">
          <span>Order ID</span>
          <input
            placeholder="ORD-…, MAN_…, broker or perm ID"
            value={draft.order_id}
            maxLength={128}
            onChange={(e) => set('order_id', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>Trade ID</span>
          <input
            placeholder="MBG-… / TRD_…"
            value={draft.trade_id}
            maxLength={128}
            onChange={(e) => set('trade_id', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>Position ID</span>
          <input
            placeholder="Pair / lot trade ID"
            title="Positions are identified by their trade ID"
            value={draft.position_id}
            maxLength={128}
            onChange={(e) => set('position_id', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>Correlation ID</span>
          <input
            placeholder="Request or operation ID"
            value={draft.correlation_id}
            maxLength={128}
            onChange={(e) => set('correlation_id', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>Records</span>
          <select
            value={draft.provenance}
            onChange={(e) => set('provenance', e.target.value as AuditFilters['provenance'])}
          >
            <option value="all">All records</option>
            <option value="native">Full-context records only</option>
            <option value="legacy">Legacy imports only</option>
          </select>
        </label>
        {draft.ref_id && (
          <span className="audit-chip">
            Other identifier: <span className="mono">{draft.ref_id}</span>
            <button type="button" aria-label="Remove identifier filter" onClick={() => set('ref_id', '')}>
              ✕
            </button>
          </span>
        )}
      </Disclosure>

      <div className="audit-filter-actions">
        <span className="audit-spacer" />
        <button type="button" className="history-filter-btn" onClick={onClear}>
          Clear all
        </button>
        <button type="submit" className="reconcile-refresh-btn">
          Search
        </button>
      </div>
    </form>
  )
}
