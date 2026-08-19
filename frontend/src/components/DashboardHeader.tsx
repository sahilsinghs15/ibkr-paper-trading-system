import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { usePnlStore } from '../store/pnlStore'
import { TZ_IN, TZ_NY, type DisplayTimezone } from '../types/position'
import {
  formatInTz,
  fmtTime,
  saveTimezone,
  tzLongLabel,
  tzShortLabel,
} from '../utils/format'

export function DashboardHeader() {
  const location = useLocation()
  const active = usePnlStore((s) => s.active)
  const closed = usePnlStore((s) => s.closed)
  const streamState = usePnlStore((s) => s.streamState)
  const lastTs = usePnlStore((s) => s.lastTs)
  const displayTz = usePnlStore((s) => s.displayTz)
  const setDisplayTz = usePnlStore((s) => s.setDisplayTz)
  const [clock, setClock] = useState(() => formatInTz(new Date(), displayTz))

  useEffect(() => {
    const tick = () => setClock(formatInTz(new Date(), displayTz))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [displayTz])

  const rows = [...Object.values(active), ...Object.values(closed)]
  const strategies = [
    ...new Set(rows.map((r) => r.strategy_id).filter(Boolean)),
  ] as string[]
  const accounts = [
    ...new Set(rows.map((r) => r.ibkr_account).filter(Boolean)),
  ] as string[]
  const strategyLabel = (strategies[0] || 'model_blue')
    .toUpperCase()
    .replaceAll('_', ' ')

  let streamClass = 'warn'
  let streamText = 'STREAM CONNECTING'
  if (streamState === 'LIVE') {
    streamClass = 'ok'
    streamText = 'STREAM LIVE'
  } else if (streamState === 'RECONNECTING') {
    streamText = 'STREAM RECONNECTING'
  }

  function onTz(tz: DisplayTimezone) {
    setDisplayTz(tz)
    saveTimezone(tz)
  }

  return (
    <header>
      <div className="brand">
        <h1>{strategyLabel}</h1>
        <span className="tag">LIVE PAPER</span>
        <span className="acct">{accounts[0] || '—'}</span>
      </div>
      <div className="header-right">
        <nav className="app-nav" aria-label="Main">
          <Link to="/" className={location.pathname === '/' ? 'on' : undefined}>
            Positions
          </Link>
          <Link
            to="/settings"
            className={location.pathname === '/settings' ? 'on' : undefined}
          >
            Settings
          </Link>
        </nav>
        <div className="status-row">
        <span className="dot ok">
          <i />
          PAPER
        </span>
        <span className={`dot ${streamClass}`}>
          <i />
          {streamText}
        </span>
        <span className="dot dim">
          <i />
          LAST UPDATE{' '}
          {lastTs ? fmtTime(lastTs, displayTz, { withZone: true }) : '—'}
        </span>
        <div className="tz-box">
          <span className="tz-clock">
            {clock.time} <span>{tzLongLabel(displayTz)}</span>
          </span>
          <div className="tz-toggle" role="group" aria-label="Display timezone">
            <button
              type="button"
              className={displayTz === TZ_NY ? 'on' : undefined}
              onClick={() => onTz(TZ_NY)}
            >
              NY
            </button>
            <button
              type="button"
              className={displayTz === TZ_IN ? 'on' : undefined}
              onClick={() => onTz(TZ_IN)}
            >
              IN
            </button>
          </div>
        </div>
        </div>
      </div>
    </header>
  )
}

export function streamHint(streamState: string): string {
  return streamState === 'LIVE'
    ? 'STREAM ● LIVE'
    : `STREAM ● ${streamState}`
}

export function timeColLabel(displayTz: DisplayTimezone): string {
  return 'TIME · ' + tzShortLabel(displayTz)
}

export function closeTimeColLabel(displayTz: DisplayTimezone): string {
  return 'CLOSE TIME · ' + tzShortLabel(displayTz)
}
