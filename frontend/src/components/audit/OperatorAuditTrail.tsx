import { useCallback, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import {
  auditErrorMessage,
  fetchAuditFacets,
  searchAuditEvents,
} from '../../api/auditApi'
import { useActiveIbkrAccount } from '../../hooks/useActiveIbkrAccount'
import { usePnlStore } from '../../store/pnlStore'
import { EMPTY_AUDIT_FILTERS, type AuditFilters } from '../../types/audit'
import { tzLongLabel, tzShortLabel } from '../../utils/format'
import { AuditEventDrawer } from './AuditEventDrawer'
import { AuditEventTable } from './AuditEventTable'
import { AuditFiltersPanel } from './AuditFiltersPanel'

type Sort = 'newest' | 'oldest'

export function OperatorAuditTrail() {
  const displayTz = usePnlStore((s) => s.displayTz)
  const activeAccount = useActiveIbkrAccount()
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [sort, setSort] = useState<Sort>('newest')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const facetsQuery = useQuery({
    queryKey: ['audit-facets'],
    queryFn: fetchAuditFacets,
    staleTime: 60_000,
  })

  const searchQuery = useQuery({
    queryKey: ['audit-events', filters, page, pageSize, sort, displayTz],
    queryFn: () =>
      searchAuditEvents(
        filters,
        { limit: pageSize, offset: (page - 1) * pageSize, sort },
        displayTz,
      ),
    placeholderData: keepPreviousData,
    staleTime: 0,
  })

  const applyFilters = useCallback((next: AuditFilters) => {
    setFilters(next)
    setPage(1)
  }, [])

  const pivot = useCallback((patch: Partial<AuditFilters>, nextSort?: Sort) => {
    setFilters((f) => ({ ...f, ...patch }))
    setPage(1)
    if (nextSort) setSort(nextSort)
    setSelectedId(null)
  }, [])

  const total = searchQuery.data?.total ?? 0
  const items = searchQuery.data?.items ?? []
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1
  const end = Math.min(page * pageSize, total)
  const scopedToSession = Boolean(filters.session_id)

  return (
    <section className="audit-trail">
      <AuditFiltersPanel
        applied={filters}
        facets={facetsQuery.data}
        activeAccount={activeAccount}
        displayTz={displayTz}
        tzLabel={`Times in ${tzLongLabel(displayTz)}`}
        onApply={applyFilters}
        onClear={() => applyFilters(EMPTY_AUDIT_FILTERS)}
      />

      {scopedToSession && (
        <div className="audit-callout">
          Session timeline: every audited action performed in session{' '}
          <span className="mono">{filters.session_id}</span>, oldest first.
        </div>
      )}

      {searchQuery.error && (
        <div className="status-badge off reconcile-alert">{auditErrorMessage(searchQuery.error)}</div>
      )}

      <div className="audit-toolbar">
        <span>
          {searchQuery.isFetching ? 'Searching… ' : ''}
          {total.toLocaleString()} event{total === 1 ? '' : 's'}
          {total > 0 ? ` · showing ${start}–${end}` : ''} · times in {tzShortLabel(displayTz)}
        </span>
        <span className="audit-spacer" />
        <label className="audit-inline">
          <span>Order</span>
          <select value={sort} onChange={(e) => { setSort(e.target.value as Sort); setPage(1) }}>
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first (timeline)</option>
          </select>
        </label>
        <button
          type="button"
          className="reconcile-refresh-btn"
          onClick={() => void searchQuery.refetch()}
          disabled={searchQuery.isFetching}
        >
          Refresh
        </button>
      </div>

      <AuditEventTable
        items={items}
        loading={searchQuery.isLoading}
        displayTz={displayTz}
        selectedId={selectedId}
        onSelect={setSelectedId}
      />

      <div className="audit-pagination">
        <label className="audit-inline">
          <span>Per page</span>
          <select
            value={pageSize}
            onChange={(e) => {
              setPageSize(Number(e.target.value))
              setPage(1)
            }}
          >
            <option value={25}>25</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={200}>200</option>
          </select>
        </label>
        <button
          type="button"
          className="history-filter-btn"
          disabled={page <= 1}
          onClick={() => setPage((p) => Math.max(1, p - 1))}
        >
          ◀ Previous
        </button>
        <span>
          Page {page} of {totalPages}
        </span>
        <button
          type="button"
          className="history-filter-btn"
          disabled={page >= totalPages}
          onClick={() => setPage((p) => p + 1)}
        >
          Next ▶
        </button>
      </div>

      {selectedId && (
        <AuditEventDrawer
          eventId={selectedId}
          displayTz={displayTz}
          onClose={() => setSelectedId(null)}
          onPivot={pivot}
          onOpenEvent={setSelectedId}
        />
      )}
    </section>
  )
}
