import { useCallback, useEffect, useState } from 'react'
import { fetchAuditLogs } from '../api/auditLogsApi'
import type { AuditLogItem } from '../types/auditLog'

const CATEGORIES = [
  { id: '', label: 'All Events' },
  { id: 'orders', label: 'Orders & Baskets' },
  { id: 'signals', label: 'Signals & Webhooks' },
  { id: 'risk', label: 'Risk & RMS' },
  { id: 'reconcile', label: 'Reconciliation' },
  { id: 'positions', label: 'Positions' },
  { id: 'system', label: 'System Services' },
]

export function AuditLogsPage() {
  const [category, setCategory] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)

  const [items, setItems] = useState<AuditLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedLog, setSelectedLog] = useState<AuditLogItem | null>(null)
  const [copied, setCopied] = useState(false)

  const [refreshTrigger, setRefreshTrigger] = useState(0)

  useEffect(() => {
    let active = true
    const run = async () => {
      try {
        setLoading(true)
        setError(null)
        const offset = (page - 1) * pageSize
        const res = await fetchAuditLogs({
          category: category || undefined,
          search: search || undefined,
          date_from: dateFrom ? new Date(dateFrom).toISOString() : undefined,
          date_to: dateTo ? new Date(dateTo).toISOString() : undefined,
          limit: pageSize,
          offset,
        })
        if (active) {
          setItems(res.events || [])
          setTotal(res.total || 0)
        }
      } catch (err: unknown) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to load audit logs')
        }
      } finally {
        if (active) {
          setLoading(false)
        }
      }
    }
    void run()
    return () => {
      active = false
    }
  }, [category, search, dateFrom, dateTo, page, pageSize, refreshTrigger])

  const loadData = useCallback(() => {
    setRefreshTrigger((v) => v + 1)
  }, [])

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setSearch(searchInput.trim())
    setPage(1)
  }

  const handleCategoryChange = (catId: string) => {
    setCategory(catId)
    setPage(1)
  }

  const handleClearFilters = () => {
    setCategory('')
    setSearchInput('')
    setSearch('')
    setDateFrom('')
    setDateTo('')
    setPage(1)
  }

  const totalPages = Math.ceil(total / pageSize) || 1
  const startIdx = total === 0 ? 0 : (page - 1) * pageSize + 1
  const endIdx = Math.min(page * pageSize, total)

  const copyJson = (data: unknown) => {
    navigator.clipboard.writeText(JSON.stringify(data, null, 2))
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <main className="page audit-logs-page" style={{ padding: '16px 24px', maxWidth: '1400px', margin: '0 auto' }}>
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', flexWrap: 'wrap', gap: '12px' }}>
        <div>
          <h1 style={{ fontSize: '20px', fontWeight: 700, margin: 0, color: 'var(--ink)' }}>System Audit Logs</h1>
          <span style={{ fontSize: '12px', color: 'var(--muted)' }}>
            Immutable event audit trail from event_log (server-side paginated, newest-first)
          </span>
        </div>
        <button
          type="button"
          className="reconcile-refresh-btn"
          onClick={() => void loadData()}
          disabled={loading}
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      {/* Category Tabs */}
      <div style={{ display: 'flex', gap: '6px', overflowX: 'auto', paddingBottom: '8px', marginBottom: '12px' }}>
        {CATEGORIES.map((cat) => (
          <button
            key={cat.id}
            type="button"
            className={`history-filter-btn ${category === cat.id ? 'active' : ''}`}
            onClick={() => handleCategoryChange(cat.id)}
            style={{ fontSize: '11px', padding: '5px 12px' }}
          >
            {cat.label}
          </button>
        ))}
      </div>

      {/* Search and Date Filter Bar */}
      <div style={{
        display: 'flex',
        gap: '12px',
        alignItems: 'center',
        flexWrap: 'wrap',
        background: 'var(--panel)',
        padding: '12px',
        borderRadius: '6px',
        border: '1px solid var(--line)',
        marginBottom: '16px',
      }}>
        <form onSubmit={handleSearchSubmit} style={{ display: 'flex', gap: '8px', flex: '1 1 300px' }}>
          <input
            type="text"
            placeholder="Search kind, process, symbol, account, message..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            style={{
              flex: 1,
              padding: '6px 10px',
              fontSize: '12px',
              background: 'var(--panel-2)',
              border: '1px solid var(--line)',
              borderRadius: '4px',
              color: 'var(--ink)',
            }}
          />
          <button
            type="submit"
            className="reconcile-refresh-btn"
            style={{ padding: '6px 12px' }}
          >
            Search
          </button>
        </form>

        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--muted)' }}>
          <span>From:</span>
          <input
            type="date"
            value={dateFrom}
            onChange={(e) => {
              setDateFrom(e.target.value)
              setPage(1)
            }}
            style={{
              padding: '4px 8px',
              fontSize: '12px',
              background: 'var(--panel-2)',
              border: '1px solid var(--line)',
              borderRadius: '4px',
              color: 'var(--ink)',
            }}
          />
          <span>To:</span>
          <input
            type="date"
            value={dateTo}
            onChange={(e) => {
              setDateTo(e.target.value)
              setPage(1)
            }}
            style={{
              padding: '4px 8px',
              fontSize: '12px',
              background: 'var(--panel-2)',
              border: '1px solid var(--line)',
              borderRadius: '4px',
              color: 'var(--ink)',
            }}
          />
        </div>

        {(category || search || dateFrom || dateTo) && (
          <button
            type="button"
            onClick={handleClearFilters}
            style={{
              padding: '5px 10px',
              fontSize: '11px',
              background: 'transparent',
              border: '1px solid var(--line)',
              borderRadius: '4px',
              color: 'var(--dim)',
              cursor: 'pointer',
            }}
          >
            Clear Filters
          </button>
        )}
      </div>

      {error && (
        <div className="status-badge off reconcile-alert" style={{ marginBottom: '16px' }}>
          {error}
        </div>
      )}

      {/* Table */}
      <div className="board factory-board scrollable-table-container" style={{ border: '1px solid var(--line)', borderRadius: '6px', overflowX: 'auto' }}>
        <table className="factory-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
          <thead>
            <tr>
              <th style={{ width: '15%' }}>TIMESTAMP</th>
              <th style={{ width: '10%' }}>PROCESS</th>
              <th style={{ width: '18%' }}>KIND</th>
              <th style={{ width: '15%' }}>TARGET / SYMBOL</th>
              <th style={{ width: '34%' }}>SUMMARY / DETAIL</th>
              <th style={{ width: '8%', textAlign: 'center' }}>ACTION</th>
            </tr>
          </thead>
          <tbody>
            {loading && items.length === 0 ? (
              <tr>
                <td colSpan={6} style={{ textAlign: 'center', padding: '32px', color: 'var(--muted)' }}>
                  Loading audit logs…
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr>
                <td colSpan={6} style={{ textAlign: 'center', padding: '32px', color: 'var(--dim)' }}>
                  No event log records match the current filter.
                </td>
              </tr>
            ) : (
              items.map((row) => {
                const ts = new Date(row.ts)
                const tsFormatted = Number.isNaN(ts.getTime()) ? row.ts : ts.toLocaleString()
                const d = row.detail || {}
                const sym = String(d.symbol || d.trading_symbol || '')
                const acc = String(d.ibkr_account || d.account_id || '')
                const targetText = [sym, acc].filter(Boolean).join(' · ') || (row.order_id ? `Order #${row.order_id}` : '—')

                let summary = String(d.message || d.title || d.action || d.reason || '')
                if (!summary) {
                  if (row.kind === 'POSITION_RECONCILE') {
                    summary = `run #${d.run_id ?? ''} — ${d.match_count ?? 0} matches, ${d.drift_count ?? 0} drift, ${d.ghost_count ?? 0} ghost`
                  } else {
                    summary = Object.keys(d).length > 0 ? Object.entries(d).slice(0, 3).map(([k, v]) => `${k}=${String(v)}`).join(', ') : '—'
                  }
                }

                return (
                  <tr
                    key={row.id}
                    className="factory-row factory-row-clickable"
                    onClick={() => setSelectedLog(row)}
                  >
                    <td className="mono" style={{ whiteSpace: 'nowrap', color: 'var(--ink)' }}>{tsFormatted}</td>
                    <td>
                      <span style={{
                        display: 'inline-block',
                        padding: '2px 6px',
                        borderRadius: '3px',
                        fontSize: '10px',
                        fontWeight: 600,
                        background: 'var(--panel-2)',
                        border: '1px solid var(--line)',
                        color: 'var(--ink)',
                      }}>
                        {row.process}
                      </span>
                    </td>
                    <td style={{ fontWeight: 600 }}>
                      <span style={{
                        color: row.kind.includes('DETECTED') || row.kind.includes('STOPPED') || row.kind.includes('REJECT') || row.kind.includes('ERROR')
                          ? '#ff6b6b'
                          : row.kind.includes('RESOLVED') || row.kind.includes('STARTED') || row.kind.includes('FILLED')
                          ? 'var(--green)'
                          : 'var(--ink)',
                      }}>
                        {row.kind}
                      </span>
                    </td>
                    <td className="mono" style={{ color: 'var(--dim)' }}>{targetText}</td>
                    <td style={{ maxWidth: '350px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--muted)' }}>
                      {summary}
                    </td>
                    <td style={{ textAlign: 'center' }}>
                      <button
                        type="button"
                        className="reconcile-refresh-btn"
                        style={{ padding: '3px 8px', fontSize: '10px' }}
                        onClick={(e) => {
                          e.stopPropagation()
                          setSelectedLog(row)
                        }}
                      >
                        Detail
                      </button>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Server-Side Pagination Bar */}
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        marginTop: '16px',
        fontSize: '12px',
        color: 'var(--muted)',
        flexWrap: 'wrap',
        gap: '12px',
      }}>
        <div>
          Showing {startIdx}–{endIdx} of {total.toLocaleString()} records
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span>Per page:</span>
          <select
            value={pageSize}
            onChange={(e) => {
              setPageSize(Number(e.target.value))
              setPage(1)
            }}
            style={{
              padding: '4px 6px',
              fontSize: '12px',
              background: 'var(--panel)',
              border: '1px solid var(--line)',
              borderRadius: '4px',
              color: 'var(--ink)',
            }}
          >
            <option value={25}>25</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
          </select>

          <button
            type="button"
            className="history-filter-btn"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            style={{ padding: '4px 10px', fontSize: '11px', opacity: page <= 1 ? 0.4 : 1 }}
          >
            ◀ Previous
          </button>
          <span>Page {page} of {totalPages}</span>
          <button
            type="button"
            className="history-filter-btn"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            style={{ padding: '4px 10px', fontSize: '11px', opacity: page >= totalPages ? 0.4 : 1 }}
          >
            Next ▶
          </button>
        </div>
      </div>

      {/* Detail Drawer Modal */}
      {selectedLog && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.65)',
            backdropFilter: 'blur(3px)',
            display: 'flex',
            justifyContent: 'flex-end',
            zIndex: 1000,
          }}
          onClick={() => setSelectedLog(null)}
        >
          <div
            style={{
              width: '100%',
              maxWidth: '650px',
              background: 'var(--panel)',
              borderLeft: '1px solid var(--line)',
              height: '100%',
              display: 'flex',
              flexDirection: 'column',
              boxShadow: '-4px 0 20px rgba(0, 0, 0, 0.5)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{
              padding: '16px 20px',
              borderBottom: '1px solid var(--line)',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}>
              <div>
                <h3 style={{ margin: 0, fontSize: '16px', fontWeight: 700, color: 'var(--ink)' }}>
                  Event Log #{selectedLog.id}
                </h3>
                <span style={{ fontSize: '11px', color: 'var(--muted)' }}>
                  {new Date(selectedLog.ts).toISOString()}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setSelectedLog(null)}
                style={{
                  background: 'transparent',
                  border: 'none',
                  fontSize: '18px',
                  color: 'var(--dim)',
                  cursor: 'pointer',
                  padding: '4px 8px',
                }}
              >
                ✕
              </button>
            </div>

            <div style={{ padding: '16px 20px', overflowY: 'auto', flex: 1 }}>
              <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: '8px', fontSize: '12px', marginBottom: '16px' }}>
                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Process:</span>
                <span className="mono">{selectedLog.process}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Kind:</span>
                <span className="mono" style={{ fontWeight: 700 }}>{selectedLog.kind}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Signal ID:</span>
                <span className="mono">{selectedLog.signal_id ?? '—'}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Order ID:</span>
                <span className="mono">{selectedLog.order_id ?? '—'}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Basket ID:</span>
                <span className="mono">{selectedLog.basket_id ?? '—'}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Idempotency:</span>
                <span className="mono" style={{ wordBreak: 'break-all' }}>{selectedLog.idempotency_key ?? '—'}</span>
              </div>

              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontSize: '12px', fontWeight: 700, color: 'var(--ink)' }}>Payload / Detail JSON</span>
                <button
                  type="button"
                  onClick={() => copyJson(selectedLog.detail)}
                  style={{
                    padding: '3px 8px',
                    fontSize: '11px',
                    background: 'var(--panel-2)',
                    border: '1px solid var(--line)',
                    borderRadius: '4px',
                    color: 'var(--ink)',
                    cursor: 'pointer',
                  }}
                >
                  {copied ? '✓ Copied' : 'Copy JSON'}
                </button>
              </div>

              <pre style={{
                background: '#0a0d14',
                padding: '12px',
                borderRadius: '6px',
                border: '1px solid var(--line)',
                fontSize: '11px',
                fontFamily: 'var(--mono)',
                color: '#a5d6ff',
                overflowX: 'auto',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                maxHeight: '400px',
              }}>
                {JSON.stringify(selectedLog.detail, null, 2)}
              </pre>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
