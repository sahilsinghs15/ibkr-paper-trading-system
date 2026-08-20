import { useEffect, useState } from 'react'
import { usePnlStore } from '../store/pnlStore'
import { getCanonicalStatus, type SignalItem, useSignalStore } from '../store/signalStore'
import { isSoundEnabled, toggleSoundEnabled, unlockAudioContext } from '../utils/audioNotification'
import { displayStrategy, fmtTime } from '../utils/format'

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

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || isNaN(seconds) || seconds < 0) {
    return '—'
  }
  if (seconds < 60) {
    return `${seconds.toFixed(1)} seconds`
  }
  const mins = Math.floor(seconds / 60)
  const secs = (seconds % 60).toFixed(1)
  return `${mins}m ${secs}s`
}

function computeProcessingDuration(sig: SignalItem): { text: string; isActive: boolean } {
  if (sig.processing_duration_sec !== undefined && sig.processing_duration_sec !== null) {
    return { text: formatDuration(sig.processing_duration_sec), isActive: false }
  }
  if (sig.received_at && sig.processed_at) {
    const tRec = new Date(sig.received_at).getTime()
    const tProc = new Date(sig.processed_at).getTime()
    if (!isNaN(tRec) && !isNaN(tProc) && tProc >= tRec) {
      return { text: formatDuration((tProc - tRec) / 1000), isActive: false }
    }
  }
  if (sig.is_active_processing || getCanonicalStatus(sig) === 'PROCESSING') {
    if (sig.received_at) {
      const tRec = new Date(sig.received_at).getTime()
      const tNow = Date.now()
      if (!isNaN(tRec) && tNow >= tRec) {
        return { text: `${((tNow - tRec) / 1000).toFixed(1)} seconds`, isActive: true }
      }
    }
    return { text: 'Active processing', isActive: true }
  }
  return { text: '—', isActive: false }
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

interface TimelineItem {
  ts: string
  dotColor: 'green' | 'amber' | 'red' | 'blue'
  description: string
}

function buildUnifiedTimeline(sig: SignalItem): TimelineItem[] {
  const events: TimelineItem[] = []
  const baseTs = sig.received_at || new Date().toISOString()

  // 1. Signal Received
  events.push({
    ts: baseTs,
    dotColor: 'blue',
    description: `TradingView signal received for ${sig.pair || 'Pair'} (${sig.action || 'OPEN'})`,
  })

  // 2. Risk Check & OMS Order Submission
  const primaryOrders = (sig.orders || []).filter((o) => !o.is_compensation)
  if (sig.reject_reason) {
    events.push({
      ts: sig.processed_at || baseTs,
      dotColor: 'red',
      description: `RMS Risk Check Rejected: ${cleanRejectReason(sig.reject_reason)}`,
    })
  } else if (primaryOrders.length > 0) {
    events.push({
      ts: baseTs,
      dotColor: 'amber',
      description: `RMS Risk Checks PASSED — OMS submitted ${primaryOrders.length} leg order(s) to IBKR broker adapter`,
    })
  }

  // 3. Execution Fill Events
  for (const ord of sig.orders || []) {
    const execs = ord.executions || []
    if (execs.length > 0) {
      for (const ex of execs) {
        events.push({
          ts: ex.executed_at || ord.filled_at || baseTs,
          dotColor: 'green',
          description: `Fill report for ${ex.symbol} (${ex.side}): +${ex.quantity} shares @ $${ex.price}`,
        })
      }
    } else if (ord.fill_qty > 0) {
      events.push({
        ts: ord.filled_at || baseTs,
        dotColor: 'green',
        description: `Leg ${ord.symbol} (${ord.buy_sell}): ${ord.fill_qty} / ${ord.quantity} filled ${ord.fill_price ? `@ $${ord.fill_price}` : ''} (${ord.status})`,
      })
    }
  }

  // 4. Audit Log Events (Retries, Unwinding)
  for (const ev of sig.events || []) {
    const ts = ev.ts || baseTs
    const kind = String(ev.kind || '').toUpperCase()
    const detail = (ev.detail || {}) as Record<string, unknown>

    if (kind === 'BASKET_RETRY') {
      const attempt = detail.attempt ? `Attempt #${detail.attempt} of 3` : 'Retry'
      const rem = detail.remaining_qty !== undefined ? ` (${detail.remaining_qty} remaining)` : ''
      events.push({
        ts,
        dotColor: 'amber',
        description: `Execution Retry Triggered: ${attempt}${rem} — ${detail.reason || 'Fill wait timeout reached'}`,
      })
    } else if (kind === 'BASKET_UNWINDING') {
      events.push({
        ts,
        dotColor: 'red',
        description: 'Naked-Pair Protection Activated: Incomplete leg timeout reached. Automatically squaring off filled exposure.',
      })
    }
  }

  // 5. Final Processing State
  if (sig.processed_at) {
    events.push({
      ts: sig.processed_at,
      dotColor: 'green',
      description: `Signal lifecycle processing completed (${sig.canonical_status || sig.status})`,
    })
  }

  // Sort strictly by ISO timestamp string
  events.sort((a, b) => a.ts.localeCompare(b.ts))
  return events
}

export function SignalTrayTable({ accountFilter }: { accountFilter?: string }) {
  const signals = useSignalStore((s) => s.signals)
  const isLoading = useSignalStore((s) => s.isLoading)
  const page = useSignalStore((s) => s.page)
  const pageSize = useSignalStore((s) => s.pageSize)
  const total = useSignalStore((s) => s.total)
  const totalPages = useSignalStore((s) => s.totalPages)
  const counts = useSignalStore((s) => s.counts)
  const fetchSignals = useSignalStore((s) => s.fetchSignals)
  const setPage = useSignalStore((s) => s.setPage)
  const storeSetStatusFilter = useSignalStore((s) => s.setStatusFilter)

  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()

  const [activeStatus, setActiveStatus] = useState<string>('ALL')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [soundOn, setSoundOn] = useState(() => isSoundEnabled())

  useEffect(() => {
    fetchSignals({ page: 1, account: cleanFilter })
  }, [cleanFilter, fetchSignals])

  const handleToggleSound = () => {
    unlockAudioContext()
    const next = toggleSoundEnabled()
    setSoundOn(next)
  }

  const handleFilterClick = (status: string) => {
    setActiveStatus(status)
    storeSetStatusFilter(status, cleanFilter)
  }

  const toggleExpand = (idKey: string) => {
    setExpandedId((prev) => (prev === idKey ? null : idKey))
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
            aria-label={`All Signals (${total})`}
          >
            ALL ({total})
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
                const isExpanded = expandedId === idKey
                const act = String(sig.action || 'OPEN').toUpperCase()
                const isRejected = isRejectedSig(sig)
                const isAccepted = isAcceptedSig(sig)
                const fillInfo = computeFillSummary(sig)
                const timeline = buildUnifiedTimeline(sig)
                const procDuration = computeProcessingDuration(sig)

                const orders = sig.orders || []
                const primaryOrders = orders.filter((o) => !o.is_compensation)
                const compensationOrders = orders.filter((o) => o.is_compensation)
                const retryEvents = (sig.events || []).filter((e) => String(e.kind).toUpperCase() === 'BASKET_RETRY')

                const incompleteLeg = primaryOrders.find((o) => (Number(o.fill_qty) || 0) < (Number(o.quantity) || 0))

                return (
                  <tr
                    key={idKey}
                    className={`signal-table-row ${isRejected ? 'row-rejected' : isAccepted ? 'row-accepted' : 'row-processing'} ${isExpanded ? 'expanded' : ''}`}
                    onClick={() => toggleExpand(idKey)}
                    title="Click row to inspect complete leg execution breakdown, retry history, and protection lifecycle"
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
                        {act === 'OPEN' ? '⚡ OPEN PAIR' : '🔒 CLOSE PAIR'}
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
                          '✓ ACCEPTED'
                        ) : isRejected ? (
                          '✕ REJECTED'
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
                        {isRejected ? (
                          <span className="txt-error">{cleanRejectReason(sig.reject_reason)}</span>
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
                        <span className="expand-indicator">{isExpanded ? '▲ hide details' : '▼ expand details'}</span>
                      </div>

                      {/* Expandable Technical Lifecycle Drawer */}
                      {isExpanded && (
                        <div className="signal-expanded-drawer" onClick={(e) => e.stopPropagation()}>
                          {/* 1. TOTAL PROCESSING TIME METRIC CARD */}
                          <div className="drawer-section processing-time-card">
                            <h5 className="drawer-section-title">⏱ TOTAL SIGNAL PROCESSING TIME</h5>
                            <div className="processing-time-body">
                              <div className="time-metric-box">
                                <span className="time-metric-label dim-txt font-bold">
                                  {procDuration.isActive ? 'CURRENTLY PROCESSING FOR:' : 'TOTAL PROCESSING TIME:'}
                                </span>
                                <span className={`time-metric-val font-bold ${procDuration.isActive ? 'txt-warning' : 'txt-success'}`}>
                                  {procDuration.text}
                                </span>
                              </div>
                              <div className="time-stamps-row dim-txt mono">
                                <span>Received: {fmtTime(sig.received_at, displayTz)}</span>
                                {sig.processed_at && <span>Completed: {fmtTime(sig.processed_at, displayTz)}</span>}
                              </div>
                            </div>
                          </div>

                          {/* 2. LEG-BY-LEG EXECUTION CARDS */}
                          {primaryOrders.length > 0 && (
                            <div className="drawer-section">
                              <h5 className="drawer-section-title">📊 LEG-BY-LEG EXECUTION STATE</h5>
                              <div className="leg-cards-grid">
                                {primaryOrders.map((ord, idx) => {
                                  const req = Number(ord.quantity) || 0
                                  const fill = Number(ord.fill_qty) || 0
                                  const rem = Math.max(0, req - fill)
                                  const isLegFull = fill >= req && req > 0
                                  const isLegPartial = fill > 0 && fill < req
                                  const execs = ord.executions || []

                                  return (
                                    <div
                                      key={ord.id || ord.internal_order_id}
                                      className={`leg-execution-card ${isLegFull ? 'full' : isLegPartial ? 'partial' : 'pending'}`}
                                    >
                                      <div className="leg-card-header">
                                        <span className="leg-name font-bold">
                                          LEG {idx + 1} — {ord.symbol} ({ord.buy_sell})
                                        </span>
                                        <span className={`leg-status-tag ${isLegFull ? 'full' : isLegPartial ? 'partial' : 'pending'}`}>
                                          {isLegFull ? '✓ FILLED' : isLegPartial ? '⟳ RETRYING' : '● SUBMITTED'}
                                        </span>
                                      </div>

                                      <div className="leg-card-body">
                                        <div className="leg-qty-row font-bold">
                                          <span>Filled: {fill} / {req}</span>
                                          {rem > 0 && <span className="txt-warning">Remaining: {rem}</span>}
                                        </div>

                                        {/* Visual Progress Bar */}
                                        <div className="leg-progress-track">
                                          <div
                                            className={`leg-progress-bar ${isLegFull ? 'green' : 'amber'}`}
                                            style={{ width: `${Math.min(100, (fill / (req || 1)) * 100)}%` }}
                                          />
                                        </div>

                                        {/* Multi-step execution fill history */}
                                        {execs.length > 0 && (
                                          <div className="fill-history-sublist dim-txt mono">
                                            <span className="fill-history-title font-bold">Fill History:</span>
                                            {execs.map((ex) => (
                                              <div key={ex.id || ex.exec_id}>
                                                • {fmtTime(ex.executed_at, displayTz)}: +{ex.quantity} @ ${ex.price}
                                              </div>
                                            ))}
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            </div>
                          )}

                          {/* 3. RETRY POLICY & CURRENT ACTION BLOCK */}
                          {!isAccepted && !isRejected && (
                            <div className="drawer-section retry-policy-card">
                              <h5 className="drawer-section-title">🔄 RETRY POLICY & CURRENT ACTION</h5>
                              <div className="retry-card-content">
                                <div className="retry-status-item">
                                  <span className="dim-txt font-bold">Active Retry Policy:</span>{' '}
                                  <span className="mono">
                                    {retryEvents.length > 0
                                      ? `${fillInfo.latestAttempt || retryEvents.length} of 3 attempts executed`
                                      : 'Initial fill wait window (10s)'}
                                  </span>
                                </div>
                                <div className="retry-status-item">
                                  <span className="dim-txt font-bold">Current System Action:</span>{' '}
                                  <span className={incompleteLeg ? 'txt-warning font-bold' : 'txt-success'}>
                                    {incompleteLeg
                                      ? `Waiting for ${incompleteLeg.symbol} to fill (${Math.max(0, Number(incompleteLeg.quantity) - Number(incompleteLeg.fill_qty))} remaining)`
                                      : 'All legs filled — Completing basket execution'}
                                  </span>
                                </div>
                              </div>
                            </div>
                          )}

                          {/* 4. AUTOMATIC PROTECTION / SQUARE-OFF CARD */}
                          {compensationOrders.length > 0 && (
                            <div className="drawer-section protection-section">
                              <h5 className="drawer-section-title txt-warning">
                                🛡 AUTOMATIC NAKED-PAIR PROTECTION
                              </h5>
                              <div className="protection-explanation">
                                <p className="txt-warning font-bold">
                                  ⚠ PROTECTION TRIGGERED — {incompleteLeg ? incompleteLeg.symbol : 'Incomplete leg'} could not be fully filled within the configured retry window.
                                </p>
                                <p className="dim-txt">
                                  Automatically squaring off filled exposure to prevent an unbalanced naked position on account capital.
                                </p>
                              </div>

                              <div className="compensation-orders-block">
                                <span className="dim-txt font-bold">Square-Off Compensation Orders:</span>
                                <table className="drawer-leg-table">
                                  <thead>
                                    <tr>
                                      <th>ORDER ID</th>
                                      <th>SYMBOL</th>
                                      <th>SIDE</th>
                                      <th>SQUARE-OFF QTY</th>
                                      <th>FILLED</th>
                                      <th>STATUS</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {compensationOrders.map((ord) => (
                                      <tr key={ord.id || ord.internal_order_id}>
                                        <td className="mono">{ord.internal_order_id || ord.id}</td>
                                        <td className="font-bold">{ord.symbol}</td>
                                        <td className={`mono ${ord.buy_sell === 'BUY' ? 'txt-green' : 'txt-red'}`}>
                                          {ord.buy_sell}
                                        </td>
                                        <td className="mono">{ord.quantity}</td>
                                        <td className="mono font-bold">{ord.fill_qty}</td>
                                        <td>
                                          <span className="leg-status-tag full">
                                            ✓ {ord.status || 'EXECUTED'}
                                          </span>
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>

                              <div className="final-exposure-result font-bold txt-warning">
                                FINAL RESULT: PAIR SQUARED OFF (Net Pair Exposure: Flat)
                              </div>
                            </div>
                          )}

                          {/* 5. UNIFIED CHRONOLOGICAL LIFECYCLE TIMELINE */}
                          <div className="drawer-section">
                            <h5 className="drawer-section-title">⏱ UNIFIED CHRONOLOGICAL LIFECYCLE TIMELINE</h5>
                            <div className="lifecycle-timeline">
                              {timeline.map((item, idx) => (
                                <div className="timeline-item" key={`item-${idx}`}>
                                  <span className={`timeline-dot ${item.dotColor}`} />
                                  <span className="timeline-time mono">{fmtTime(item.ts, displayTz)}</span>
                                  <span className="timeline-desc">{item.description}</span>
                                </div>
                              ))}
                            </div>
                          </div>

                          {/* 6. TECHNICAL IDENTIFIERS */}
                          <div className="drawer-section technical-metadata">
                            <div className="drawer-grid">
                              <div>
                                <span className="drawer-label">Signal ID:</span> <span className="mono">{sig.signal_id}</span>
                              </div>
                              <div>
                                <span className="drawer-label">Trade ID:</span> <span className="mono">{sig.trade_id || '—'}</span>
                              </div>
                              <div>
                                <span className="drawer-label">Raw Status:</span> <span className="mono">{sig.status}</span>
                              </div>
                              <div>
                                <span className="drawer-label">Canonical:</span> <span className="mono">{sig.canonical_status || '—'}</span>
                              </div>
                            </div>
                          </div>
                        </div>
                      )}
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
    </div>
  )
}
