import { useMemo, useState } from 'react'
import { usePnlStore } from '../store/pnlStore'
import { useSignalStore } from '../store/signalStore'
import { displayStrategy, fmtTime } from '../utils/format'
import { Pagination } from './Pagination'

function cleanRejectReason(raw: string | null | undefined): string {
  if (!raw) return 'Signal declined by execution pipeline'
  const s = String(raw).trim()
  if (s.includes('NO_OPEN_POSITION')) {
    return 'Cannot close: No active open position matching this trade ID'
  }
  if (s.includes('ambiguous') || s.includes('code=200')) {
    return 'Broker Error: IBKR contract description ambiguous'
  }
  if (s.includes('RMS') || s.includes('CHECK')) {
    return 'Blocked by RMS: Strategy risk limit exceeded'
  }
  if (s.includes('COMMITTED_NOT_CONFIGURED')) {
    return 'Allocation Error: Account capital not configured'
  }
  return s.replace(/^[A-Z_]+:\s*/, '').trim()
}

export function SignalTray({ accountFilter }: { accountFilter?: string }) {
  const signals = useSignalStore((s) => s.signals)
  const streamState = usePnlStore((s) => s.streamState)
  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()

  const [statusFilter, setStatusFilter] = useState<'ALL' | 'ACCEPTED' | 'REJECTED' | 'PENDING'>('ALL')
  const [currentPage, setCurrentPage] = useState(1)
  const pageSize = 5

  const scopedSignals = useMemo(() => {
    if (!cleanFilter) return signals
    return signals.filter((sig) => {
      if (!sig.ibkr_account) return true
      return String(sig.ibkr_account).trim().toUpperCase() === cleanFilter
    })
  }, [signals, cleanFilter])

  const filteredSignals = useMemo(() => {
    if (statusFilter === 'ACCEPTED') {
      return scopedSignals.filter(
        (s) => (s.status === 'PROCESSED' || s.status === 'FILLED' || s.status === 'SUCCESS') && !s.reject_reason
      )
    }
    if (statusFilter === 'REJECTED') {
      return scopedSignals.filter(
        (s) => s.status === 'REJECTED' || Boolean(s.reject_reason)
      )
    }
    if (statusFilter === 'PENDING') {
      return scopedSignals.filter(
        (s) => (s.status === 'NEW' || s.status === 'RECEIVED' || s.status === 'PROCESSING') && !s.reject_reason
      )
    }
    return scopedSignals
  }, [scopedSignals, statusFilter])

  const paginatedSignals = useMemo(() => {
    const start = (currentPage - 1) * pageSize
    return filteredSignals.slice(start, start + pageSize)
  }, [filteredSignals, currentPage, pageSize])

  const acceptedCount = useMemo(
    () =>
      scopedSignals.filter(
        (s) => (s.status === 'PROCESSED' || s.status === 'FILLED' || s.status === 'SUCCESS') && !s.reject_reason
      ).length,
    [scopedSignals]
  )

  const rejectedCount = useMemo(
    () => scopedSignals.filter((s) => s.status === 'REJECTED' || Boolean(s.reject_reason)).length,
    [scopedSignals]
  )

  const pendingCount = useMemo(
    () =>
      scopedSignals.filter(
        (s) => (s.status === 'NEW' || s.status === 'RECEIVED' || s.status === 'PROCESSING') && !s.reject_reason
      ).length,
    [scopedSignals]
  )

  let streamClass = 'warn'
  let streamText = 'Connecting'
  if (streamState === 'LIVE') {
    streamClass = 'ok'
    streamText = 'LIVE ●'
  } else if (streamState === 'RECONNECTING') {
    streamText = 'RECONNECTING'
  }

  return (
    <aside className="signal-tray-panel">
      <div className="signal-tray-header">
        <div className="signal-tray-title-group">
          <h3>SIGNAL TRAY</h3>
          <span className={`signal-stream-pill ${streamClass}`}>{streamText}</span>
        </div>
        <span className="signal-count-badge" title="Total signals in tray">{scopedSignals.length}</span>
      </div>

      {/* Filter Tabs */}
      <div className="signal-tray-filters">
        <button
          type="button"
          className={`signal-filter-btn ${statusFilter === 'ALL' ? 'active' : ''}`}
          onClick={() => {
            setStatusFilter('ALL')
            setCurrentPage(1)
          }}
        >
          ALL ({scopedSignals.length})
        </button>
        <button
          type="button"
          className={`signal-filter-btn green ${statusFilter === 'ACCEPTED' ? 'active' : ''}`}
          onClick={() => {
            setStatusFilter('ACCEPTED')
            setCurrentPage(1)
          }}
        >
          ✓ ACC ({acceptedCount})
        </button>
        <button
          type="button"
          className={`signal-filter-btn red ${statusFilter === 'REJECTED' ? 'active' : ''}`}
          onClick={() => {
            setStatusFilter('REJECTED')
            setCurrentPage(1)
          }}
        >
          ✕ REJ ({rejectedCount})
        </button>
        <button
          type="button"
          className={`signal-filter-btn amber ${statusFilter === 'PENDING' ? 'active' : ''}`}
          onClick={() => {
            setStatusFilter('PENDING')
            setCurrentPage(1)
          }}
        >
          ● NEW ({pendingCount})
        </button>
      </div>

      <div className="signal-tray-list">
        {filteredSignals.length === 0 ? (
          <div className="signal-empty-state">
            <span className="empty-icon">📡</span>
            <p>{statusFilter === 'ALL' ? 'No signals received yet.' : `No ${statusFilter.toLowerCase()} signals.`}</p>
            <span className="dim-txt">Incoming TradingView alert webhooks will appear here in real time.</span>
          </div>
        ) : (
          paginatedSignals.map((sig) => {
            const act = String(sig.action || 'OPEN').toUpperCase()
            const st = String(sig.status || 'NEW').toUpperCase()
            const isRejected = st === 'REJECTED' || Boolean(sig.reject_reason)
            const isAccepted = (st === 'PROCESSED' || st === 'FILLED' || st === 'SUCCESS') && !isRejected

            return (
              <div
                className={`signal-card ${isRejected ? 'rejected' : isAccepted ? 'accepted' : 'processing'}`}
                key={sig.signal_id || sig.id}
              >
                {/* Header: Action + Time */}
                <div className="signal-card-header">
                  <span className={`signal-action-badge ${act.toLowerCase()}`}>
                    {act === 'OPEN' ? '⚡ OPEN PAIR' : '🔒 CLOSE PAIR'}
                  </span>
                  <span className="signal-time">{fmtTime(sig.received_at, displayTz)}</span>
                </div>

                {/* Body: Pair Symbol */}
                <div className="signal-card-body">
                  <div className="signal-pair-group">
                    <span className="signal-pair-symbol">{sig.pair || '—'}</span>
                  </div>
                  <span className="signal-strategy-name">{displayStrategy(sig.strategy_id)}</span>
                </div>

                {/* Outcome Banner */}
                <div className={`signal-outcome-banner ${isRejected ? 'error' : isAccepted ? 'success' : 'info'}`}>
                  {isAccepted ? (
                    <span>✓ ACCEPTED — Executed on {sig.ibkr_account || 'Account'}</span>
                  ) : isRejected ? (
                    <span>✕ REJECTED: {cleanRejectReason(sig.reject_reason)}</span>
                  ) : (
                    <span>● PROCESSING — Evaluating RMS & OMS</span>
                  )}
                </div>

                {/* Footer: Identifiers */}
                <div className="signal-card-footer">
                  <span className="signal-id mono" title={sig.signal_id}>
                    ID: {sig.trade_id || sig.signal_id}
                  </span>
                </div>
              </div>
            )
          })
        )}
      </div>

      <Pagination
        currentPage={currentPage}
        totalItems={filteredSignals.length}
        pageSize={pageSize}
        onPageChange={setCurrentPage}
        compact
      />
    </aside>
  )
}
