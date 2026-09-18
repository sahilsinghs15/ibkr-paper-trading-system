import { useMemo, useState } from 'react'
import type { AuditFacets, AuditFilters } from '../../types/audit'
import { localInputValue } from './auditFormat'

interface Props {
  applied: AuditFilters
  facets: AuditFacets | undefined
  activeAccount: string | null
  onApply: (filters: AuditFilters) => void
  onClear: () => void
}

const QUICK_RANGES: { label: string; ms: number }[] = [
  { label: '1h', ms: 3_600_000 },
  { label: '24h', ms: 86_400_000 },
  { label: '7d', ms: 7 * 86_400_000 },
  { label: '30d', ms: 30 * 86_400_000 },
]

/**
 * Investigation filters. Edits are drafted locally and applied together so a
 * multi-criteria query (who + where + what + when) runs as one server search.
 */
export function AuditFiltersPanel({ applied, facets, activeAccount, onApply, onClear }: Props) {
  const [draft, setDraft] = useState<AuditFilters>(applied)
  const [showMore, setShowMore] = useState(
    Boolean(applied.session_id || applied.device_id || applied.ref_id || applied.provenance !== 'all'),
  )
  const [syncedFrom, setSyncedFrom] = useState<AuditFilters>(applied)

  // Pivots from the detail drawer change `applied`; mirror them into the draft.
  if (syncedFrom !== applied) {
    setSyncedFrom(applied)
    setDraft(applied)
    if (applied.session_id || applied.device_id || applied.ref_id) setShowMore(true)
  }

  const actionOptions = useMemo(() => {
    const cats = facets?.categories ?? []
    const visible = draft.categories.length
      ? cats.filter((c) => draft.categories.includes(c.category))
      : cats
    return visible
  }, [facets, draft.categories])

  const set = <K extends keyof AuditFilters>(key: K, value: AuditFilters[K]) =>
    setDraft((d) => ({ ...d, [key]: value }))

  const toggleCategory = (category: string) =>
    setDraft((d) => {
      const categories = d.categories.includes(category)
        ? d.categories.filter((c) => c !== category)
        : [...d.categories, category]
      const actionStillValid =
        !d.action ||
        (facets?.categories ?? []).some(
          (c) =>
            (categories.length === 0 || categories.includes(c.category)) &&
            c.actions.some((a) => a.action === d.action),
        )
      return { ...d, categories, action: actionStillValid ? d.action : '' }
    })

  const quickRange = (ms: number) => {
    const now = new Date()
    setDraft((d) => ({
      ...d,
      date_from: localInputValue(new Date(now.getTime() - ms)),
      date_to: '',
    }))
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    onApply(draft)
  }

  return (
    <form className="audit-filters" onSubmit={submit}>
      <div className="audit-filter-row">
        <label className="audit-field">
          <span>From (local)</span>
          <input
            type="datetime-local"
            value={draft.date_from}
            onChange={(e) => set('date_from', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>To (local)</span>
          <input
            type="datetime-local"
            value={draft.date_to}
            onChange={(e) => set('date_to', e.target.value)}
          />
        </label>
        <div className="audit-quick">
          {QUICK_RANGES.map((r) => (
            <button key={r.label} type="button" className="history-filter-btn" onClick={() => quickRange(r.ms)}>
              Last {r.label}
            </button>
          ))}
        </div>
        <label className="audit-field grow">
          <span>Keyword</span>
          <input
            type="search"
            placeholder="summary, order/trade id, symbol, reason…"
            value={draft.q}
            maxLength={200}
            onChange={(e) => set('q', e.target.value)}
          />
        </label>
      </div>

      <div className="audit-filter-row">
        <label className="audit-field grow">
          <span>User (e-mail)</span>
          <input
            list="audit-actor-options"
            placeholder="alice@example.com or part of it"
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
        <label className="audit-field">
          <span>Source IP / CIDR</span>
          <input
            placeholder="203.0.113.7 or 10.0.0.0/8"
            value={draft.ip}
            maxLength={64}
            onChange={(e) => set('ip', e.target.value)}
          />
        </label>
        <label className="audit-field">
          <span>Account</span>
          <div className="audit-inline">
            <input
              placeholder="DU1234567 or id"
              value={draft.account}
              maxLength={64}
              onChange={(e) => set('account', e.target.value)}
            />
            {activeAccount && (
              <button
                type="button"
                className="history-filter-btn"
                title="Filter to the account currently selected in the dashboard"
                onClick={() => set('account', activeAccount)}
              >
                Current
              </button>
            )}
          </div>
        </label>
        <label className="audit-field">
          <span>Result</span>
          <select value={draft.result} onChange={(e) => set('result', e.target.value)}>
            <option value="">Any</option>
            {(facets?.results ?? []).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="audit-filter-row">
        <div className="audit-chips" role="group" aria-label="Categories">
          {(facets?.categories ?? []).map((c) => (
            <button
              key={c.category}
              type="button"
              aria-pressed={draft.categories.includes(c.category)}
              className={`history-filter-btn ${draft.categories.includes(c.category) ? 'active' : ''}`}
              onClick={() => toggleCategory(c.category)}
            >
              {c.label}
            </button>
          ))}
        </div>
        <label className="audit-field grow">
          <span>Action</span>
          <select value={draft.action} onChange={(e) => set('action', e.target.value)}>
            <option value="">Any action</option>
            {actionOptions.map((c) => (
              <optgroup key={c.category} label={c.label}>
                {c.actions.map((a) => (
                  <option key={a.action} value={a.action}>
                    {a.label}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
      </div>

      {showMore && (
        <div className="audit-filter-row">
          <label className="audit-field grow">
            <span>Session ID</span>
            <input
              placeholder="auth session UUID"
              value={draft.session_id}
              onChange={(e) => set('session_id', e.target.value)}
            />
          </label>
          <label className="audit-field">
            <span>Device ID (client-reported)</span>
            <input value={draft.device_id} maxLength={64} onChange={(e) => set('device_id', e.target.value)} />
          </label>
          <label className="audit-field">
            <span>Related ID</span>
            <input
              placeholder="order / trade / operation id"
              value={draft.ref_id}
              maxLength={128}
              onChange={(e) => set('ref_id', e.target.value)}
            />
          </label>
          <label className="audit-field">
            <span>Records</span>
            <select
              value={draft.provenance}
              onChange={(e) => set('provenance', e.target.value as AuditFilters['provenance'])}
            >
              <option value="all">All</option>
              <option value="native">Native (full context)</option>
              <option value="legacy">Legacy imports</option>
            </select>
          </label>
        </div>
      )}

      <div className="audit-filter-actions">
        <button type="button" className="audit-link" onClick={() => setShowMore((v) => !v)}>
          {showMore ? 'Fewer filters' : 'More filters (session, device, related id)'}
        </button>
        <span className="audit-spacer" />
        <button type="button" className="history-filter-btn" onClick={onClear}>
          Clear
        </button>
        <button type="submit" className="reconcile-refresh-btn">
          Search
        </button>
      </div>
    </form>
  )
}
