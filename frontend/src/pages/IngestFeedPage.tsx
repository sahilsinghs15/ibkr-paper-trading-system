import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchIngestJobs } from '../api/ingestJobsApi'
import { usePnlStore } from '../store/pnlStore'
import type { IngestJobCounts, IngestJobItem, IngestJobsResponse } from '../types/ingestJob'
import { normalizeIbkrAccount } from '../utils/activeAccount'
import { displayStrategy, fmtTime } from '../utils/format'

const STATUS_CHIPS: { id: string; label: string; countKey: keyof IngestJobCounts }[] = [
  { id: '', label: 'ALL', countKey: 'all' },
  { id: 'QUEUED', label: 'QUEUED', countKey: 'queued' },
  { id: 'PROCESSING', label: 'PROCESSING', countKey: 'processing' },
  { id: 'COMPLETED', label: 'COMPLETED', countKey: 'completed' },
  { id: 'REJECTED', label: 'REJECTED', countKey: 'rejected' },
  { id: 'FAILED', label: 'FAILED', countKey: 'failed' },
  { id: 'DEFERRED', label: 'DEFERRED', countKey: 'deferred' },
  { id: 'RECOVERY', label: 'RECOVERY', countKey: 'recovery' },
  { id: 'DEAD_LETTER', label: 'DEAD LETTER', countKey: 'dead_letter' },
]

function renderStatusBadge(status: string) {
  const s = status.toUpperCase()
  if (s === 'COMPLETED') {
    return (
      <span
        className="status-pill-badge accepted"
        style={{
          background: 'rgba(62, 207, 142, 0.12)',
          color: 'var(--green)',
          border: '1px solid rgba(62, 207, 142, 0.3)',
        }}
      >
        COMPLETED
      </span>
    )
  }
  if (s === 'QUEUED' || s === 'RECEIVED') {
    return (
      <span
        className="status-pill-badge"
        style={{
          background: 'rgba(235, 179, 56, 0.12)',
          color: 'var(--amber)',
          border: '1px solid rgba(235, 179, 56, 0.3)',
        }}
      >
        {s}
      </span>
    )
  }
  if (s === 'PROCESSING' || s === 'CLAIMED') {
    return (
      <span
        className="status-pill-badge processing"
        style={{
          background: 'rgba(56, 189, 248, 0.12)',
          color: '#38bdf8',
          border: '1px solid rgba(56, 189, 248, 0.3)',
        }}
      >
        <span
          className="spin-icon"
          style={{ display: 'inline-block', animation: 'spin 1s linear infinite' }}
        >
          ⟳
        </span>{' '}
        {s}
      </span>
    )
  }
  if (s.includes('DEFER')) {
    return (
      <span
        className="status-pill-badge"
        style={{
          background: 'rgba(99, 102, 241, 0.12)',
          color: '#818cf8',
          border: '1px solid rgba(99, 102, 241, 0.3)',
        }}
      >
        {s}
      </span>
    )
  }
  return (
    <span
      className="status-pill-badge rejected"
      style={{
        background: 'rgba(239, 107, 115, 0.12)',
        color: 'var(--red)',
        border: '1px solid rgba(239, 107, 115, 0.3)',
      }}
    >
      {s}
    </span>
  )
}

function getErrorSummary(job: IngestJobItem): string {
  const err = job.last_error || job.deferral_reason
  if (!err) return '—'
  const firstLine = err.split('\n')[0].trim()
  return firstLine || '—'
}

export function IngestFeedPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount)
  const displayTz = usePnlStore((s) => s.displayTz)

  const [status, setStatus] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)

  const [data, setData] = useState<IngestJobsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedJob, setSelectedJob] = useState<IngestJobItem | null>(null)
  const [bodyTab, setBodyTab] = useState<'parsed' | 'raw'>('parsed')
  const [copied, setCopied] = useState(false)
  const [refreshTrigger, setRefreshTrigger] = useState(0)

  useEffect(() => {
    let active = true
    const run = async (isPoll = false) => {
      if (!isPoll) setLoading(true)
      try {
        setError(null)
        const res = await fetchIngestJobs({
          page,
          page_size: 50,
          status: status || undefined,
          ibkr_account: cleanAccount || undefined,
          search: search || undefined,
        })
        if (active) {
          setData(res)
        }
      } catch (err: unknown) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to load ingest jobs')
        }
      } finally {
        if (active && !isPoll) {
          setLoading(false)
        }
      }
    }

    void run(false)
    const interval = setInterval(() => {
      void run(true)
    }, 5000)

    return () => {
      active = false
      clearInterval(interval)
    }
  }, [page, status, cleanAccount, search, refreshTrigger])

  const handleManualRefresh = useCallback(() => {
    setRefreshTrigger((v) => v + 1)
  }, [])

  const handleStatusChange = (newStatus: string) => {
    setStatus(newStatus)
    setPage(1)
  }

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setSearch(searchInput.trim())
    setPage(1)
  }

  const handleClearSearch = () => {
    setSearchInput('')
    setSearch('')
    setPage(1)
  }

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const jobs = data?.jobs || []
  const total = data?.total || 0
  const totalPages = data?.total_pages || 1
  const counts = data?.counts

  return (
    <main
      className="page ingest-feed-page"
      style={{ padding: '16px 24px', maxWidth: '1400px', margin: '0 auto' }}
    >
      <header
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '16px',
          flexWrap: 'wrap',
          gap: '12px',
        }}
      >
        <div>
          <h1 style={{ fontSize: '20px', fontWeight: 700, margin: 0, color: 'var(--ink)' }}>
            Ingest Feed
          </h1>
          <span style={{ fontSize: '12px', color: 'var(--muted)' }}>
            Raw webhook ingest stored in signal_jobs (auto-polls every 5s, newest-first)
            {cleanAccount ? ` · Scoped to ${cleanAccount}` : ' · Global feed'}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <button
            type="button"
            className="reconcile-refresh-btn"
            onClick={handleManualRefresh}
            disabled={loading}
          >
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      {/* Filter Toolbar: Status Chips & Search Input */}
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: '12px',
          background: 'var(--panel)',
          padding: '12px 16px',
          borderRadius: '6px',
          border: '1px solid var(--line)',
          marginBottom: '16px',
        }}
      >
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--dim)', marginRight: '4px' }}>
            Status:
          </span>
          {STATUS_CHIPS.map((chip) => {
            const isActive = status === chip.id
            const cnt = counts ? counts[chip.countKey] : undefined
            return (
              <button
                key={chip.id}
                type="button"
                onClick={() => handleStatusChange(chip.id)}
                style={{
                  fontSize: '11px',
                  fontWeight: 600,
                  padding: '4px 10px',
                  borderRadius: '4px',
                  border: isActive ? '1px solid var(--accent)' : '1px solid var(--line)',
                  background: isActive ? 'var(--accent)' : 'var(--panel-2)',
                  color: isActive ? '#fff' : 'var(--dim)',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                }}
              >
                <span>{chip.label}</span>
                {cnt !== undefined && (
                  <span
                    style={{
                      fontSize: '10px',
                      background: isActive ? 'rgba(255,255,255,0.2)' : 'var(--line)',
                      padding: '1px 5px',
                      borderRadius: '10px',
                    }}
                  >
                    {cnt}
                  </span>
                )}
              </button>
            )
          })}
        </div>

        <form
          onSubmit={handleSearchSubmit}
          style={{ display: 'flex', gap: '8px', alignItems: 'center' }}
        >
          <input
            type="text"
            placeholder="Search signal, trade, correlation, strategy ID…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            style={{
              flex: 1,
              maxWidth: '400px',
              padding: '6px 12px',
              fontSize: '12px',
              background: 'var(--bg)',
              border: '1px solid var(--line)',
              borderRadius: '4px',
              color: 'var(--ink)',
            }}
          />
          <button
            type="submit"
            style={{
              padding: '6px 12px',
              fontSize: '12px',
              fontWeight: 600,
              background: 'var(--panel-2)',
              border: '1px solid var(--line)',
              borderRadius: '4px',
              color: 'var(--ink)',
              cursor: 'pointer',
            }}
          >
            Search
          </button>
          {search && (
            <button
              type="button"
              onClick={handleClearSearch}
              style={{
                padding: '6px 12px',
                fontSize: '12px',
                background: 'transparent',
                border: 'none',
                color: 'var(--dim)',
                cursor: 'pointer',
              }}
            >
              Clear
            </button>
          )}
        </form>
      </div>

      {error && (
        <div
          style={{
            padding: '12px 16px',
            background: 'rgba(239, 107, 115, 0.1)',
            border: '1px solid var(--red)',
            borderRadius: '6px',
            color: 'var(--red)',
            marginBottom: '16px',
            fontSize: '13px',
          }}
        >
          {error}
        </div>
      )}

      {/* Main Table or Empty State */}
      <div
        style={{
          background: 'var(--panel)',
          borderRadius: '6px',
          border: '1px solid var(--line)',
          overflow: 'hidden',
        }}
      >
        {jobs.length === 0 && !loading ? (
          <div style={{ padding: '48px 24px', textAlign: 'center', color: 'var(--muted)' }}>
            <p style={{ fontSize: '14px', margin: 0 }}>
              No ingest jobs yet. TradingView POSTs to :8000 appear here as soon as they are queued.
            </p>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table
              className="table"
              style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '12px' }}
            >
              <thead>
                <tr style={{ borderBottom: '1px solid var(--line)', background: 'var(--panel-2)' }}>
                  <th style={{ padding: '10px 14px', color: 'var(--dim)', fontWeight: 600 }}>RECEIVED</th>
                  <th style={{ padding: '10px 14px', color: 'var(--dim)', fontWeight: 600 }}>STATUS</th>
                  <th style={{ padding: '10px 14px', color: 'var(--dim)', fontWeight: 600 }}>ACTION</th>
                  <th style={{ padding: '10px 14px', color: 'var(--dim)', fontWeight: 600 }}>SIGNAL / TRADE</th>
                  <th style={{ padding: '10px 14px', color: 'var(--dim)', fontWeight: 600 }}>STRATEGY</th>
                  <th style={{ padding: '10px 14px', color: 'var(--dim)', fontWeight: 600 }}>ACCOUNT</th>
                  <th style={{ padding: '10px 14px', color: 'var(--dim)', fontWeight: 600 }}>ERROR</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((job) => (
                  <tr
                    key={job.job_id}
                    onClick={() => {
                      setSelectedJob(job)
                      setBodyTab('parsed')
                    }}
                    style={{
                      borderBottom: '1px solid var(--line)',
                      cursor: 'pointer',
                      transition: 'background 0.15s',
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.background = 'var(--panel-2)'
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.background = 'transparent'
                    }}
                  >
                    <td style={{ padding: '10px 14px', whiteSpace: 'nowrap' }} className="mono">
                      {fmtTime(job.received_at, displayTz)}
                    </td>
                    <td style={{ padding: '10px 14px', whiteSpace: 'nowrap' }}>
                      {renderStatusBadge(job.status)}
                    </td>
                    <td style={{ padding: '10px 14px', whiteSpace: 'nowrap' }}>
                      <span
                        style={{
                          fontWeight: 700,
                          color: job.action === 'OPEN' ? 'var(--green)' : 'var(--amber)',
                        }}
                      >
                        {job.action}
                      </span>
                    </td>
                    <td style={{ padding: '10px 14px' }}>
                      <div className="mono" style={{ fontWeight: 600, color: 'var(--ink)' }}>
                        {job.signal_id}
                      </div>
                      {job.trade_id && (
                        <div className="mono" style={{ fontSize: '11px', color: 'var(--dim)' }}>
                          {job.trade_id}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: '10px 14px', whiteSpace: 'nowrap' }}>
                      {displayStrategy(job.strategy_id)}
                    </td>
                    <td style={{ padding: '10px 14px', whiteSpace: 'nowrap' }}>
                      <span className="mono">{job.ibkr_account || 'unscoped'}</span>
                    </td>
                    <td
                      style={{
                        padding: '10px 14px',
                        maxWidth: '280px',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        color: job.last_error ? 'var(--red)' : job.deferral_reason ? 'var(--amber)' : 'var(--dim)',
                      }}
                    >
                      {getErrorSummary(job)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination Footer */}
        {total > 0 && (
          <div
            className="signal-pagination-bar"
            style={{
              padding: '12px 16px',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              borderTop: '1px solid var(--line)',
            }}
          >
            <span className="pagination-info dim-txt mono" style={{ fontSize: '12px' }}>
              Showing {Math.min((page - 1) * 50 + 1, total)}–{Math.min(page * 50, total)} of {total} jobs
            </span>
            <div className="pagination-buttons" style={{ display: 'flex', gap: '6px' }}>
              <button
                type="button"
                className="page-nav-btn"
                disabled={page <= 1 || loading}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                ← Previous
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1)
                .slice(Math.max(0, page - 3), Math.min(totalPages, page + 2))
                .map((p) => (
                  <button
                    key={p}
                    type="button"
                    className={`page-num-btn ${p === page ? 'active' : ''}`}
                    onClick={() => setPage(p)}
                  >
                    {p}
                  </button>
                ))}
              <button
                type="button"
                className="page-nav-btn"
                disabled={page >= totalPages || loading}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                Next →
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Selected Job Drawer */}
      {selectedJob && (
        <div
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(0, 0, 0, 0.65)',
            zIndex: 1000,
            display: 'flex',
            justifyContent: 'flex-end',
          }}
          onClick={() => setSelectedJob(null)}
        >
          <div
            style={{
              width: '600px',
              maxWidth: '92vw',
              height: '100%',
              backgroundColor: 'var(--panel)',
              borderLeft: '1px solid var(--line)',
              display: 'flex',
              flexDirection: 'column',
              boxShadow: '-4px 0 24px rgba(0,0,0,0.5)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Drawer Header */}
            <div
              style={{
                padding: '16px 20px',
                borderBottom: '1px solid var(--line)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <div>
                <h3 style={{ margin: 0, fontSize: '16px', fontWeight: 700, color: 'var(--ink)' }}>
                  Ingest Job Details
                </h3>
                <span style={{ fontSize: '11px', color: 'var(--muted)' }} className="mono">
                  {selectedJob.job_id}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setSelectedJob(null)}
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

            {/* Drawer Content */}
            <div style={{ padding: '16px 20px', overflowY: 'auto', flex: 1 }}>
              {/* Meta Grid */}
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: '130px 1fr',
                  gap: '8px',
                  fontSize: '12px',
                  marginBottom: '16px',
                  background: 'var(--panel-2)',
                  padding: '12px',
                  borderRadius: '6px',
                  border: '1px solid var(--line)',
                }}
              >
                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Status:</span>
                <div>{renderStatusBadge(selectedJob.status)}</div>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Signal ID:</span>
                <span className="mono">{selectedJob.signal_id}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Trade ID:</span>
                <span className="mono">{selectedJob.trade_id ?? '—'}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Correlation ID:</span>
                <span className="mono" style={{ wordBreak: 'break-all' }}>
                  {selectedJob.correlation_id}
                </span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Idempotency Key:</span>
                <span className="mono" style={{ wordBreak: 'break-all' }}>
                  {selectedJob.idempotency_key}
                </span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Account Scope:</span>
                <span className="mono">
                  {selectedJob.account_scope
                    ? `${selectedJob.account_scope} (${selectedJob.ibkr_account ?? 'unresolved'})`
                    : 'NULL (unscoped/global)'}
                </span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Attempts:</span>
                <span className="mono">
                  {selectedJob.attempt_count} / {selectedJob.max_attempts}
                </span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Received At:</span>
                <span className="mono">{selectedJob.received_at ? fmtTime(selectedJob.received_at, displayTz) : '—'}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Queued At:</span>
                <span className="mono">{selectedJob.queued_at ? fmtTime(selectedJob.queued_at, displayTz) : '—'}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Claimed At:</span>
                <span className="mono">{selectedJob.claimed_at ? fmtTime(selectedJob.claimed_at, displayTz) : '—'}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Processing Started:</span>
                <span className="mono">{selectedJob.processing_started_at ? fmtTime(selectedJob.processing_started_at, displayTz) : '—'}</span>

                <span style={{ color: 'var(--dim)', fontWeight: 600 }}>Completed At:</span>
                <span className="mono">{selectedJob.completed_at ? fmtTime(selectedJob.completed_at, displayTz) : '—'}</span>

                {selectedJob.last_error && (
                  <>
                    <span style={{ color: 'var(--red)', fontWeight: 600 }}>Last Error:</span>
                    <span className="mono" style={{ color: 'var(--red)', wordBreak: 'break-word' }}>
                      {selectedJob.last_error}
                    </span>
                  </>
                )}

                {selectedJob.deferral_reason && (
                  <>
                    <span style={{ color: 'var(--amber)', fontWeight: 600 }}>Deferral Reason:</span>
                    <span className="mono" style={{ color: 'var(--amber)', wordBreak: 'break-word' }}>
                      {selectedJob.deferral_reason}
                    </span>
                  </>
                )}
              </div>

              {/* Body Section with Tabs: Parsed JSON | Raw body */}
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  marginBottom: '8px',
                }}
              >
                <div style={{ display: 'flex', gap: '4px' }}>
                  <button
                    type="button"
                    onClick={() => setBodyTab('parsed')}
                    style={{
                      padding: '4px 10px',
                      fontSize: '11px',
                      fontWeight: 600,
                      borderRadius: '4px',
                      border: '1px solid var(--line)',
                      background: bodyTab === 'parsed' ? 'var(--accent)' : 'var(--panel-2)',
                      color: bodyTab === 'parsed' ? '#fff' : 'var(--dim)',
                      cursor: 'pointer',
                    }}
                  >
                    Parsed JSON
                  </button>
                  <button
                    type="button"
                    onClick={() => setBodyTab('raw')}
                    style={{
                      padding: '4px 10px',
                      fontSize: '11px',
                      fontWeight: 600,
                      borderRadius: '4px',
                      border: '1px solid var(--line)',
                      background: bodyTab === 'raw' ? 'var(--accent)' : 'var(--panel-2)',
                      color: bodyTab === 'raw' ? '#fff' : 'var(--dim)',
                      cursor: 'pointer',
                    }}
                  >
                    Raw body
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    if (bodyTab === 'parsed') {
                      copyText(JSON.stringify(selectedJob.parsed_json, null, 2))
                    } else {
                      copyText(selectedJob.raw_body)
                    }
                  }}
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
                  {copied ? '✓ Copied' : bodyTab === 'parsed' ? 'Copy JSON' : 'Copy Raw'}
                </button>
              </div>

              <pre
                style={{
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
                }}
              >
                {bodyTab === 'parsed'
                  ? JSON.stringify(selectedJob.parsed_json, null, 2)
                  : selectedJob.raw_body}
              </pre>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
