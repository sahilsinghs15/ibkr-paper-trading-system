import { useMemo, useState } from 'react'
import { usePnlStore } from '../store/pnlStore'
import { getCanonicalStatus, type SignalItem, useSignalStore } from '../store/signalStore'
import { isSoundEnabled, toggleSoundEnabled, unlockAudioContext } from '../utils/audioNotification'
import { displayStrategy, fmtTime } from '../utils/format'

function cleanRejectReason(raw: string | null | undefined): string {
  if (!raw) return 'Signal declined by execution pipeline'
  const s = String(raw).trim()
  if (s.includes('NO_OPEN_POSITION')) {
    return 'Cannot close: No active open position found'
  }
  if (s.includes('ambiguous') || s.includes('code=200')) {
    return 'Broker Error: IBKR contract description ambiguous'
  }
  if (s.includes('RMS') || s.includes('CHECK')) {
    return 'Blocked by RMS: Risk limit exceeded'
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

export function SignalWidget({
  accountFilter,
  onViewFullTray,
}: {
  accountFilter?: string
  onViewFullTray?: () => void
}) {
  const signals = useSignalStore((s) => s.signals)
  const counts = useSignalStore((s) => s.counts)
  const streamState = usePnlStore((s) => s.streamState)
  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()

  const [statusFilter, setStatusFilter] = useState<'PROCESSING' | 'ACCEPTED' | 'REJECTED'>('PROCESSING')

  const scopedSignals = useMemo(() => {
    if (!cleanFilter) return signals
    return signals.filter((sig) => {
      if (!sig.ibkr_account) return false
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

  let streamClass = 'warn'
  let streamText = 'Connecting'
  if (streamState === 'LIVE') {
    streamClass = 'ok'
    streamText = 'LIVE ●'
  } else if (streamState === 'RECONNECTING') {
    streamText = 'RECONNECTING'
  }

  const [soundOn, setSoundOn] = useState(() => isSoundEnabled())

  const handleToggleSound = () => {
    unlockAudioContext()
    const next = toggleSoundEnabled()
    setSoundOn(next)
  }

  return (
    <aside className="signal-widget-panel">
      <div className="signal-widget-header">
        <div className="signal-widget-title-group">
          <h3>SIGNAL MONITOR</h3>
          <span className={`signal-stream-pill ${streamClass}`}>{streamText}</span>
        </div>
        <div className="signal-widget-header-actions">
          <button
            type="button"
            className={`sound-toggle-btn ${soundOn ? 'on' : 'muted'}`}
            onClick={handleToggleSound}
            title={soundOn ? 'Signal arrival sounds enabled (click to mute)' : 'Signal arrival sounds muted (click to enable)'}
            aria-label={soundOn ? 'Mute signal sounds' : 'Enable signal sounds'}
          >
            {soundOn ? '🔊 Sound ON' : '🔇 Sound OFF'}
          </button>
          {onViewFullTray && (
            <button
              type="button"
              className="view-full-tray-link"
              onClick={onViewFullTray}
              title="Open full Signal Tray workspace"
            >
              View Signal Tray →
            </button>
          )}
        </div>
      </div>

      {/* Three Status Filter Buttons */}
      <div className="signal-widget-filters">
        <button
          type="button"
          className={`signal-filter-btn amber ${statusFilter === 'PROCESSING' ? 'active' : ''}`}
          onClick={() => setStatusFilter('PROCESSING')}
          aria-label={`Processing (${counts.processing})`}
        >
          <span className="spin-icon" aria-hidden="true">⟳</span> PROCESSING ({counts.processing})
        </button>
        <button
          type="button"
          className={`signal-filter-btn green ${statusFilter === 'ACCEPTED' ? 'active' : ''}`}
          onClick={() => setStatusFilter('ACCEPTED')}
          aria-label={`Accepted (${counts.accepted})`}
        >
          ✓ ACCEPTED ({counts.accepted})
        </button>
        <button
          type="button"
          className={`signal-filter-btn red ${statusFilter === 'REJECTED' ? 'active' : ''}`}
          onClick={() => setStatusFilter('REJECTED')}
          aria-label={`Rejected (${counts.rejected})`}
        >
          ✕ REJECTED ({counts.rejected})
        </button>
      </div>

      <div className="signal-widget-list scrollable-signal-list">
        {filteredSignals.length === 0 ? (
          <div className="signal-empty-state">
            <span className="empty-icon">📡</span>
            <p>No {statusFilter.toLowerCase()} signals.</p>
            <span className="dim-txt">Incoming TradingView alert webhooks will appear here live.</span>
          </div>
        ) : (
          filteredSignals.map((sig) => {
            const act = String(sig.action || 'OPEN').toUpperCase()
            const isRejected = isRejectedSig(sig)
            const isAccepted = isAcceptedSig(sig)

            return (
              <div
                className={`signal-card ${isRejected ? 'rejected' : isAccepted ? 'accepted' : 'processing'}`}
                key={sig.signal_id || sig.id}
              >
                <div className="signal-card-header">
                  <span className={`signal-action-badge ${act.toLowerCase()}`}>
                    {act === 'OPEN' ? '⚡ OPEN PAIR' : '🔒 CLOSE PAIR'}
                  </span>
                  <span className="signal-time">{fmtTime(sig.received_at, displayTz)}</span>
                </div>

                <div className="signal-card-body">
                  <span className="signal-pair-symbol">{sig.pair || '—'}</span>
                  <span className="signal-strategy-name">{displayStrategy(sig.strategy_id)}</span>
                </div>

                {(() => {
                  const orders = sig.orders || []
                  const primary = orders.filter((o) => !o.is_compensation)
                  const hasComp = orders.some((o) => o.is_compensation)
                  const retryEvents = (sig.events || []).filter((e) => String(e.kind).toUpperCase() === 'BASKET_RETRY')
                  let attemptStr = ''
                  if (retryEvents.length > 0) {
                    const dt = (retryEvents[retryEvents.length - 1].detail || {}) as Record<string, unknown>
                    attemptStr = ` (Retry ${dt.attempt || retryEvents.length}/3)`
                  }

                  const fillsText = primary
                    .map((o) => {
                      const req = Number(o.quantity) || 0
                      const fill = Number(o.fill_qty) || 0
                      const tag = fill >= req && req > 0 ? '✓ ' : fill > 0 ? '⟳ ' : ''
                      return `${tag}${o.symbol} ${fill}/${req}`
                    })
                    .join(' · ')

                  return (
                    <div className={`signal-outcome-banner ${isRejected ? 'error' : isAccepted ? 'success' : 'info'}`}>
                      {isRejected ? (
                        <span>✕ REJECTED: {cleanRejectReason(sig.reject_reason)}</span>
                      ) : hasComp ? (
                        <span>⚠ SQUARE-OFF — {fillsText || 'Protection activated'}</span>
                      ) : fillsText ? (
                        <span>
                          {!isAccepted && <span className="spin-icon" aria-hidden="true">⟳ </span>}
                          {fillsText}{attemptStr}
                        </span>
                      ) : isAccepted ? (
                        <span>✓ ACCEPTED — Executed on {sig.ibkr_account || 'Account'}</span>
                      ) : (
                        <span>
                          <span className="spin-icon" aria-hidden="true">⟳</span> PROCESSING — Evaluating RMS & OMS
                        </span>
                      )}
                    </div>
                  )
                })()}

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
    </aside>
  )
}
