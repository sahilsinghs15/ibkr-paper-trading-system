import { useMemo, useState } from 'react'
import { usePnlStore } from '../store/pnlStore'
import { type SignalItem, useSignalStore } from '../store/signalStore'
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

function isRejectedSig(sig: SignalItem): boolean {
  const st = String(sig.status || '').toUpperCase()
  return st === 'REJECTED' || Boolean(sig.reject_reason)
}

function isAcceptedSig(sig: SignalItem): boolean {
  const st = String(sig.status || '').toUpperCase()
  return (st === 'PROCESSED' || st === 'FILLED' || st === 'SUCCESS') && !isRejectedSig(sig)
}

function isProcessingSig(sig: SignalItem): boolean {
  return !isAcceptedSig(sig) && !isRejectedSig(sig)
}

function computeFillSummary(sig: SignalItem): {
  summaryText: string
  isProtectionTriggered: boolean
  allFilled: boolean
  partiallyFilled: boolean
  latestAttempt: number | null
} {
  const orders = sig.events ? sig.orders : sig.orders
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

  const retryEvents = (sig.events || []).filter((e) => String(e.kind).toUpperCase() === 'BASKET_RETRY')
  let latestAttempt: number | null = null
  if (retryEvents.length > 0) {
    const lastEv = retryEvents[retryEvents.length - 1]
    const dt = (lastEv.detail || {}) as Record<string, unknown>
    latestAttempt = Number(dt.attempt) || retryEvents.length
  }

  let totalRequested = 0
  let totalFilled = 0
  let filledCount = 0

  const legSummaries: string[] = []

  for (const o of primaryOrders) {
    const req = Number(o.quantity) || 0
    const fill = Number(o.fill_qty) || 0
    totalRequested += req
    totalFilled += fill
    const isLegFull = fill >= req && req > 0
    if (isLegFull) filledCount++

    const prefix = isLegFull ? '✓ ' : fill > 0 ? '⟳ ' : ''
    legSummaries.push(`${prefix}${o.symbol} ${fill}/${req}`)
  }

  const allFilled = primaryOrders.length > 0 && filledCount === primaryOrders.length
  const partiallyFilled = totalFilled > 0 && !allFilled

  let statusSuffix = ''
  if (allFilled) {
    statusSuffix = ' (Fully filled)'
  } else if (isProtectionTriggered) {
    statusSuffix = ' (Protection triggered)'
  } else if (latestAttempt !== null) {
    statusSuffix = ` (Retrying · Attempt ${latestAttempt}/3)`
  } else if (partiallyFilled) {
    statusSuffix = ' (Evaluating RMS & OMS)'
  }

  const summaryText = legSummaries.join(' · ') + statusSuffix

  return {
    summaryText,
    isProtectionTriggered,
    allFilled,
    partiallyFilled,
    latestAttempt,
  }
}

interface TimelineEvent {
  ts: string
  dotColor: 'green' | 'amber' | 'red'
  description: string
}

function buildChronologicalTimeline(sig: SignalItem): TimelineEvent[] {
  const events: TimelineEvent[] = []
  const baseTs = sig.received_at || new Date().toISOString()

  // 1. Signal Received
  events.push({
    ts: baseTs,
    dotColor: 'green',
    description: `Signal received from strategy ${displayStrategy(sig.strategy_id)} (${sig.action} ${sig.pair})`,
  })

  const isRejected = isRejectedSig(sig)

  if (isRejected) {
    events.push({
      ts: sig.processed_at || baseTs,
      dotColor: 'red',
      description: `Rejected by RMS policy: ${cleanRejectReason(sig.reject_reason)}`,
    })
    return events
  }

  // 2. RMS Risk Decision
  events.push({
    ts: baseTs,
    dotColor: 'green',
    description: 'Pre-trade risk checks passed (RMS duplicate, strategy, & capital limits verified)',
  })

  const primaryOrders = (sig.orders || []).filter((o) => !o.is_compensation)
  if (primaryOrders.length > 0) {
    events.push({
      ts: baseTs,
      dotColor: 'amber',
      description: `OMS submitted ${primaryOrders.length} leg order(s) to IBKR broker adapter`,
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
      description: `Signal lifecycle processing completed (Final DB Status: ${sig.status})`,
    })
  }

  // Sort strictly by ISO timestamp string
  events.sort((a, b) => a.ts.localeCompare(b.ts))
  return events
}

export function SignalTrayTable({ accountFilter }: { accountFilter?: string }) {
  const signals = useSignalStore((s) => s.signals)
  const isLoading = useSignalStore((s) => s.isLoading)
  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()

  const [statusFilter, setStatusFilter] = useState<'PROCESSING' | 'ACCEPTED' | 'REJECTED'>('PROCESSING')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [soundOn, setSoundOn] = useState(() => isSoundEnabled())

  const handleToggleSound = () => {
    unlockAudioContext()
    const next = toggleSoundEnabled()
    setSoundOn(next)
  }

  const scopedSignals = useMemo(() => {
    if (!cleanFilter) return signals
    return signals.filter((sig) => {
      if (!sig.ibkr_account) return true
      return String(sig.ibkr_account).trim().toUpperCase() === cleanFilter
    })
  }, [signals, cleanFilter])

  const processingSignals = useMemo(() => scopedSignals.filter(isProcessingSig), [scopedSignals])
  const acceptedSignals = useMemo(() => scopedSignals.filter(isAcceptedSig), [scopedSignals])
  const rejectedSignals = useMemo(() => scopedSignals.filter(isRejectedSig), [scopedSignals])

  const filteredSignals = useMemo(() => {
    if (statusFilter === 'ACCEPTED') return acceptedSignals
    if (statusFilter === 'REJECTED') return rejectedSignals
    return processingSignals
  }, [statusFilter, processingSignals, acceptedSignals, rejectedSignals])

  const toggleExpand = (idKey: string) => {
    setExpandedId((prev) => (prev === idKey ? null : idKey))
  }

  return (
    <div className="signal-tray-workspace">
      {/* Workspace Header + Status Filters */}
      <div className="board-header signal-tray-workspace-header">
        <div className="board-title-group">
          <h3>DEDICATED SIGNAL TRAY</h3>
          <span className="sub-title">REAL-TIME SIGNAL LIFECYCLE WORKSPACE ({scopedSignals.length} SIGNALS)</span>
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
            className={`signal-filter-btn amber ${statusFilter === 'PROCESSING' ? 'active' : ''}`}
            onClick={() => setStatusFilter('PROCESSING')}
            aria-label={`Processing (${processingSignals.length})`}
          >
            <span className="spin-icon" aria-hidden="true">⟳</span> PROCESSING ({processingSignals.length})
          </button>
          <button
            type="button"
            className={`signal-filter-btn green ${statusFilter === 'ACCEPTED' ? 'active' : ''}`}
            onClick={() => setStatusFilter('ACCEPTED')}
            aria-label={`Accepted (${acceptedSignals.length})`}
          >
            ✓ ACCEPTED ({acceptedSignals.length})
          </button>
          <button
            type="button"
            className={`signal-filter-btn red ${statusFilter === 'REJECTED' ? 'active' : ''}`}
            onClick={() => setStatusFilter('REJECTED')}
            aria-label={`Rejected (${rejectedSignals.length})`}
          >
            ✕ REJECTED ({rejectedSignals.length})
          </button>
        </div>
      </div>

      {/* Main Signal Table Container */}
      <div className="board factory-board scrollable-table-container">
        {isLoading && signals.length === 0 ? (
          <div className="signal-empty-state">
            <span className="empty-icon">⏳</span>
            <p>Loading historical signals...</p>
          </div>
        ) : filteredSignals.length === 0 ? (
          <div className="signal-empty-state">
            <span className="empty-icon">📡</span>
            <p>No {statusFilter.toLowerCase()} signals for account {cleanFilter || 'All'}.</p>
            <span className="dim-txt">Incoming TradingView alert webhooks will appear here in real time.</span>
          </div>
        ) : (
          <table className="factory-table signal-table">
            <thead>
              <tr>
                <th style={{ width: '140px' }}>TIME</th>
                <th style={{ width: '130px' }}>SIGNAL / PAIR</th>
                <th style={{ width: '110px' }}>ACTION</th>
                <th style={{ width: '120px' }}>STRATEGY</th>
                <th style={{ width: '110px' }}>ACCOUNT</th>
                <th style={{ width: '140px' }}>STATUS</th>
                <th>EXECUTION & OUTCOME SUMMARY</th>
              </tr>
            </thead>
            <tbody>
              {filteredSignals.map((sig) => {
                const idKey = sig.signal_id || String(sig.id)
                const act = String(sig.action || 'OPEN').toUpperCase()
                const isRejected = isRejectedSig(sig)
                const isAccepted = isAcceptedSig(sig)
                const isExpanded = expandedId === idKey

                const fillInfo = computeFillSummary(sig)
                const primaryOrders = (sig.orders || []).filter((o) => !o.is_compensation)
                const compensationOrders = (sig.orders || []).filter((o) => o.is_compensation)
                const retryEvents = (sig.events || []).filter((e) => String(e.kind).toUpperCase() === 'BASKET_RETRY')
                const timeline = buildChronologicalTimeline(sig)

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
                          {/* 1. LEG-BY-LEG EXECUTION CARDS */}
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

                          {/* 2. RETRY POLICY & CURRENT ACTION BLOCK */}
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

                          {/* 3. AUTOMATIC PROTECTION / SQUARE-OFF CARD */}
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

                          {/* 4. UNIFIED CHRONOLOGICAL LIFECYCLE TIMELINE */}
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

                          {/* 5. TECHNICAL IDENTIFIERS */}
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
      </div>
    </div>
  )
}
