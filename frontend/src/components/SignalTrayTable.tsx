import { useEffect, useMemo, useState } from 'react'
import { usePnlStore } from '../store/pnlStore'
import { accountMatches, getCanonicalStatus, type SignalItem, useSignalStore } from '../store/signalStore'
import { isSoundEnabled, toggleSoundEnabled, unlockAudioContext } from '../utils/audioNotification'
import { displayStrategy, fmtTime } from '../utils/format'
import { formatRejectReason } from '../utils/rejectReason'
import { SignalDetailModal } from './SignalDetailModal'

export function isRejectedSig(sig: SignalItem): boolean {
  const c = getCanonicalStatus(sig)
  return c === 'REJECTED' || c === 'SQUARE-OFF'
}

export function isAcceptedSig(sig: SignalItem): boolean {
  return getCanonicalStatus(sig) === 'ACCEPTED'
}

export function isProcessingSig(sig: SignalItem): boolean {
  return getCanonicalStatus(sig) === 'PROCESSING'
}

function computeFillSummary(sig: SignalItem): {
  summaryText: string
  isProtectionTriggered: boolean
  allFilled: boolean
  partiallyFilled: boolean
  latestAttempt: number | null
} {
  const orders = sig.orders || []
  if (!orders || orders.length === 0) {
    return {
      summaryText: '',
      isProtectionTriggered: false,
      allFilled: false,
      partiallyFilled: false,
      latestAttempt: null,
    }
  }

  const primaryOrders = orders.filter((o) => !o.is_compensation)
  const compensationOrders = orders.filter((o) => o.is_compensation)
  const isProtectionTriggered = compensationOrders.length > 0

  let latestAttempt: number | null = null
  for (const ev of sig.events || []) {
    if (String(ev.kind || '').toUpperCase() === 'BASKET_RETRY') {
      const att = Number((ev.detail || {}).attempt)
      if (att && (!latestAttempt || att > latestAttempt)) {
        latestAttempt = att
      }
    }
  }

  const legs = primaryOrders.map((o) => {
    const req = Number(o.quantity) || 0
    const fill = Number(o.fill_qty) || 0
    return { symbol: o.symbol, req, fill }
  })

  const allFilled = legs.length > 0 && legs.every((l) => l.fill >= l.req && l.req > 0)
  const partiallyFilled = legs.some((l) => l.fill > 0 && l.fill < l.req)

  const summaryParts = legs.map((l) => {
    const isFull = l.fill >= l.req && l.req > 0
    const isPart = l.fill > 0 && l.fill < l.req
    const mark = isFull ? '✓ ' : isPart ? '⟳ ' : ''
    return `${mark}${l.symbol} ${l.fill}/${l.req}`
  })

  let summaryText = summaryParts.join(' · ')
  if (latestAttempt && !allFilled && !isProtectionTriggered) {
    summaryText += ` (Retry ${latestAttempt}/3)`
  }

  return {
    summaryText,
    isProtectionTriggered,
    allFilled,
    partiallyFilled,
    latestAttempt,
  }
}

export function SignalTrayTable({ accountFilter }: { accountFilter?: string }) {
  const traySignals = useSignalStore((s) => s.traySignals)
  const isLoading = useSignalStore((s) => s.trayLoading)
  const page = useSignalStore((s) => s.trayPage)
  const pageSize = useSignalStore((s) => s.trayPageSize)
  const total = useSignalStore((s) => s.trayTotal)
  const totalPages = useSignalStore((s) => s.trayTotalPages)
  const counts = useSignalStore((s) => s.counts)
  const activeStatus = useSignalStore((s) => s.trayStatusFilter)
  const fetchTraySignals = useSignalStore((s) => s.fetchTraySignals)
  const setPage = useSignalStore((s) => s.setTrayPage)
  const setStatusFilter = useSignalStore((s) => s.setTrayStatusFilter)

  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()

  const signals = useMemo(() => {
    if (!cleanFilter) return traySignals
    return traySignals.filter((sig) => accountMatches(sig.ibkr_account, cleanFilter))
  }, [traySignals, cleanFilter])

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [soundOn, setSoundOn] = useState(() => isSoundEnabled())

  const selectedSignal = useMemo(
    () => signals.find((sig) => String(sig.signal_id || sig.id) === selectedId) ?? null,
    [signals, selectedId],
  )

  useEffect(() => {
    void fetchTraySignals({ page: 1, account: cleanFilter })
  }, [cleanFilter, fetchTraySignals])

  const handleToggleSound = () => {
    unlockAudioContext()
    const next = toggleSoundEnabled()
    setSoundOn(next)
  }

  const handleFilterClick = (status: string) => {
    void setStatusFilter(status, cleanFilter)
  }

  const openDetail = (idKey: string) => {
    setSelectedId(idKey)
  }

  const closeDetail = () => {
    setSelectedId(null)
  }

  return (
    <div className="signal-tray-workspace">
      {/* Workspace Header + Status Filters */}
      <div className="board-header signal-tray-workspace-header">
        <div className="board-title-group">
          <h3>DEDICATED SIGNAL TRAY</h3>
          <span className="sub-title">REAL-TIME SIGNAL LIFECYCLE WORKSPACE · {total} SIGNALS</span>
        </div>

        <div className="signal-tray-filters inline-filters">
          <button
            type="button"
            className={`sound-toggle-btn ${soundOn ? 'on' : 'muted'}`}
            onClick={handleToggleSound}
            title={soundOn ? 'Signal arrival sounds enabled (click to mute)' : 'Signal arrival sounds muted (click to enable)'}
            aria-label={soundOn ? 'Mute signal sounds' : 'Enable signal sounds'}
          >
            {soundOn ? '🔊 Sound ON' : '🔇 Sound OFF'}
          </button>
          <button
            type="button"
            className={`signal-filter-btn ${activeStatus === 'ALL' ? 'active' : ''}`}
            onClick={() => handleFilterClick('ALL')}
            aria-label={`All Signals (${counts.total})`}
          >
            ALL ({counts.total})
          </button>
          <button
            type="button"
            className={`signal-filter-btn amber ${activeStatus === 'PROCESSING' ? 'active' : ''}`}
            onClick={() => handleFilterClick('PROCESSING')}
            aria-label={`Processing (${counts.processing})`}
          >
            <span className="spin-icon" aria-hidden="true">⟳</span> PROCESSING ({counts.processing})
          </button>
          <button
            type="button"
            className={`signal-filter-btn green ${activeStatus === 'ACCEPTED' ? 'active' : ''}`}
            onClick={() => handleFilterClick('ACCEPTED')}
            aria-label={`Accepted (${counts.accepted})`}
          >
            ✓ ACCEPTED ({counts.accepted})
          </button>
          <button
            type="button"
            className={`signal-filter-btn red ${activeStatus === 'REJECTED' ? 'active' : ''}`}
            onClick={() => handleFilterClick('REJECTED')}
            aria-label={`Rejected (${counts.rejected})`}
          >
            ✕ REJECTED ({counts.rejected})
          </button>
        </div>
      </div>

      {/* Main Signal Tray Workspace Table */}
      <div className="board factory-board scrollable-table-container">
        {isLoading && signals.length === 0 ? (
          <div className="signal-empty-state">
            <span className="spin-icon" aria-hidden="true">⟳</span>
            <p>Loading real-time signal workspace...</p>
          </div>
        ) : signals.length === 0 ? (
          <div className="signal-empty-state">
            <span className="empty-icon">📡</span>
            <p>No {activeStatus.toLowerCase()} signals found for this account.</p>
            <span className="dim-txt">Incoming webhooks from TradingView will stream here automatically.</span>
          </div>
        ) : (
          <table className="factory-table signal-workspace-table">
            <thead>
              <tr>
                <th style={{ width: '12%' }}>RECEIVED</th>
                <th style={{ width: '15%' }}>PAIR</th>
                <th style={{ width: '10%' }}>ACTION</th>
                <th style={{ width: '13%' }}>STRATEGY</th>
                <th style={{ width: '12%' }}>ACCOUNT</th>
                <th style={{ width: '13%' }}>STATUS</th>
                <th style={{ width: '25%' }}>OUTCOME & LEG PROGRESS</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((sig: SignalItem) => {
                const idKey = String(sig.signal_id || sig.id)
                const act = String(sig.action || 'OPEN').toUpperCase()
                const isRejected = isRejectedSig(sig)
                const isAccepted = isAcceptedSig(sig)
                const fillInfo = computeFillSummary(sig)
                const rejectDisplay = isRejected
                  ? formatRejectReason(sig.reject_reason, sig.ibkr_account || cleanFilter)
                  : null

                return (
                  <tr
                    key={idKey}
                    className={`signal-table-row ${isRejected ? 'row-rejected' : isAccepted ? 'row-accepted' : 'row-processing'}`}
                    onClick={() => openDetail(idKey)}
                    title="Click row to view signal details"
                  >
                    {/* Time */}
                    <td className="cell-time mono">{fmtTime(sig.received_at, displayTz)}</td>

                    {/* Pair */}
                    <td>
                      <span className="cell-pair-symbol font-bold">{sig.pair || '—'}</span>
                    </td>

                    {/* Action */}
                    <td>
                      <span className={`signal-action-badge ${act.toLowerCase()}`}>
                        {act === 'OPEN' ? 'OPEN PAIR' : 'CLOSE PAIR'}
                      </span>
                    </td>

                    {/* Strategy */}
                    <td className="cell-strategy">{displayStrategy(sig.strategy_id)}</td>

                    {/* Account */}
                    <td className="cell-account mono">{sig.ibkr_account || cleanFilter || '—'}</td>

                    {/* Status Badge */}
                    <td>
                      <span
                        className={`status-pill-badge ${isRejected ? 'rejected' : isAccepted ? 'accepted' : 'processing'}`}
                        aria-label={isAccepted ? 'Accepted' : isRejected ? 'Rejected' : 'Processing'}
                      >
                        {fillInfo.isProtectionTriggered ? (
                          '⚠ SQUARE-OFF'
                        ) : isAccepted ? (
                          'ACCEPTED'
                        ) : isRejected ? (
                          'REJECTED'
                        ) : (
                          <>
                            <span className="spin-icon" aria-hidden="true">⟳</span> PROCESSING
                          </>
                        )}
                      </span>
                    </td>

                    {/* Outcome Detail / Reason / Leg Fill Summary */}
                    <td className="cell-reason">
                      <div className="summary-cell-content">
                        {rejectDisplay ? (
                          <div className="reject-reason-cell">
                            <span className="reject-category-pill">{rejectDisplay.category}</span>
                            <span className="txt-error reject-summary">{rejectDisplay.summary}</span>
                          </div>
                        ) : fillInfo.summaryText ? (
                          <span className={fillInfo.isProtectionTriggered ? 'txt-warning font-bold' : 'txt-success'}>
                            {fillInfo.summaryText}
                          </span>
                        ) : isAccepted ? (
                          <span className="txt-success">Executed successfully on {sig.ibkr_account || 'Account'}</span>
                        ) : (
                          <span className="txt-warning">
                            <span className="spin-icon" aria-hidden="true">⟳</span> Evaluating RMS risk policy & OMS execution...
                          </span>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}

        {/* Table Footer Pagination Controls */}
        {total > 0 && (
          <div className="signal-pagination-bar">
            <span className="pagination-info dim-txt mono">
              Showing {Math.min((page - 1) * pageSize + 1, total)}–{Math.min(page * pageSize, total)} of {total} signals
            </span>
            <div className="pagination-buttons">
              <button
                type="button"
                className="page-nav-btn"
                disabled={page <= 1 || isLoading}
                onClick={() => setPage(page - 1, cleanFilter)}
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
                    onClick={() => setPage(p, cleanFilter)}
                  >
                    {p}
                  </button>
                ))}

              <button
                type="button"
                className="page-nav-btn"
                disabled={page >= totalPages || isLoading}
                onClick={() => setPage(page + 1, cleanFilter)}
              >
                Next →
              </button>
            </div>
          </div>
        )}
      </div>

      {selectedSignal && (
        <SignalDetailModal
          sig={selectedSignal}
          accountFilter={cleanFilter}
          onClose={closeDetail}
        />
      )}
    </div>
  )
}
