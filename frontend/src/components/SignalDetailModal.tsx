import { usePnlStore } from '../store/pnlStore'
import { getCanonicalStatus, type SignalItem } from '../store/signalStore'
import { displayStrategy, fmtTime } from '../utils/format'
import { formatRejectReason } from '../utils/rejectReason'

function isRejectedSig(sig: SignalItem): boolean {
  const c = getCanonicalStatus(sig)
  return c === 'REJECTED' || c === 'SQUARE-OFF'
}

function isAcceptedSig(sig: SignalItem): boolean {
  return getCanonicalStatus(sig) === 'ACCEPTED'
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

interface TimelineItem {
  ts: string
  dotColor: 'green' | 'amber' | 'red' | 'blue'
  description: string
}

function buildUnifiedTimeline(sig: SignalItem): TimelineItem[] {
  const events: TimelineItem[] = []
  const baseTs = sig.received_at || new Date().toISOString()

  events.push({
    ts: baseTs,
    dotColor: 'blue',
    description: `TradingView signal received for ${sig.pair || 'Pair'} (${sig.action || 'OPEN'})`,
  })

  const primaryOrders = (sig.orders || []).filter((o) => !o.is_compensation)
  if (sig.reject_reason) {
    const reject = formatRejectReason(sig.reject_reason, sig.ibkr_account)
    events.push({
      ts: sig.processed_at || baseTs,
      dotColor: 'red',
      description: `Rejected — ${reject.summary}`,
    })
  } else if (primaryOrders.length > 0) {
    events.push({
      ts: baseTs,
      dotColor: 'amber',
      description: `RMS Risk Checks PASSED — OMS submitted ${primaryOrders.length} leg order(s) to IBKR broker adapter`,
    })
  }

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

  if (sig.processed_at) {
    events.push({
      ts: sig.processed_at,
      dotColor: 'green',
      description: `Signal lifecycle processing completed (${sig.canonical_status || sig.status})`,
    })
  }

  events.sort((a, b) => a.ts.localeCompare(b.ts))
  return events
}

function computeRetryInfo(sig: SignalItem) {
  const retryEvents = (sig.events || []).filter((e) => String(e.kind).toUpperCase() === 'BASKET_RETRY')
  let latestAttempt: number | null = null
  for (const ev of retryEvents) {
    const att = Number((ev.detail || {}).attempt)
    if (att && (!latestAttempt || att > latestAttempt)) {
      latestAttempt = att
    }
  }
  return { retryEvents, latestAttempt: latestAttempt || retryEvents.length || null }
}

export function SignalDetailModal({
  sig,
  accountFilter,
  onClose,
}: {
  sig: SignalItem
  accountFilter?: string
  onClose: () => void
}) {
  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()

  const act = String(sig.action || 'OPEN').toUpperCase()
  const isRejected = isRejectedSig(sig)
  const isAccepted = isAcceptedSig(sig)
  const procDuration = computeProcessingDuration(sig)
  const timeline = buildUnifiedTimeline(sig)
  const { retryEvents, latestAttempt } = computeRetryInfo(sig)

  const orders = sig.orders || []
  const primaryOrders = orders.filter((o) => !o.is_compensation)
  const compensationOrders = orders.filter((o) => o.is_compensation)
  const incompleteLeg = primaryOrders.find((o) => (Number(o.fill_qty) || 0) < (Number(o.quantity) || 0))
  const rejectDisplay = isRejected
    ? formatRejectReason(sig.reject_reason, sig.ibkr_account || cleanFilter)
    : null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card signal-detail-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>
            {sig.pair || 'Signal'} · {act === 'OPEN' ? 'OPEN PAIR' : 'CLOSE PAIR'}
          </h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="modal-body signal-detail-modal-body">
          <div className="signal-detail-meta dim-txt">
            <span>{displayStrategy(sig.strategy_id)}</span>
            <span className="mono">{sig.ibkr_account || cleanFilter || '—'}</span>
            <span>{fmtTime(sig.received_at, displayTz)}</span>
          </div>

          {rejectDisplay && (
            <div className="drawer-section reject-detail-section">
              <h5 className="drawer-section-title">REJECTION</h5>
              <p className="reject-drawer-summary txt-error">{rejectDisplay.summary}</p>
              <pre className="reject-raw-block mono">{rejectDisplay.raw}</pre>
            </div>
          )}

          <div className="drawer-section processing-time-card">
            <h5 className="drawer-section-title">TOTAL SIGNAL PROCESSING TIME</h5>
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

          {primaryOrders.length > 0 && (
            <div className="drawer-section">
              <h5 className="drawer-section-title">LEG-BY-LEG EXECUTION STATE</h5>
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
                          {isLegFull ? 'FILLED' : isLegPartial ? 'RETRYING' : 'SUBMITTED'}
                        </span>
                      </div>

                      <div className="leg-card-body">
                        <div className="leg-qty-row font-bold">
                          <span>Filled: {fill} / {req}</span>
                          {rem > 0 && <span className="txt-warning">Remaining: {rem}</span>}
                        </div>

                        <div className="leg-progress-track">
                          <div
                            className={`leg-progress-bar ${isLegFull ? 'green' : 'amber'}`}
                            style={{ width: `${Math.min(100, (fill / (req || 1)) * 100)}%` }}
                          />
                        </div>

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

          {!isAccepted && !isRejected && (
            <div className="drawer-section retry-policy-card">
              <h5 className="drawer-section-title">RETRY POLICY & CURRENT ACTION</h5>
              <div className="retry-card-content">
                <div className="retry-status-item">
                  <span className="dim-txt font-bold">Active Retry Policy:</span>{' '}
                  <span className="mono">
                    {retryEvents.length > 0
                      ? `${latestAttempt || retryEvents.length} of 3 attempts executed`
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

          {compensationOrders.length > 0 && (
            <div className="drawer-section protection-section">
              <h5 className="drawer-section-title txt-warning">AUTOMATIC NAKED-PAIR PROTECTION</h5>
              <div className="protection-explanation">
                <p className="txt-warning font-bold">
                  PROTECTION TRIGGERED — {incompleteLeg ? incompleteLeg.symbol : 'Incomplete leg'} could not be fully filled within the configured retry window.
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
                          <span className="leg-status-tag full">{ord.status || 'EXECUTED'}</span>
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

          <div className="drawer-section">
            <h5 className="drawer-section-title">UNIFIED CHRONOLOGICAL LIFECYCLE TIMELINE</h5>
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
      </div>
    </div>
  )
}
