import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { usePnlStore } from '../store/pnlStore'
import { useAuthStore } from '../store/authStore'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'
import { TZ_IN, TZ_NY, type DisplayTimezone } from '../types/position'
import {
  formatInTz,
  fmtTime,
  saveTimezone,
  tzLongLabel,
} from '../utils/format'
import { AppNav } from './AppNav'

export function AppHeader() {
  const currentAccount = useActiveIbkrAccount()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()

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

  let streamClass = 'warn'
  let streamText = 'Connecting'
  if (streamState === 'LIVE') {
    streamClass = 'ok'
    streamText = 'Live'
  } else if (streamState === 'RECONNECTING') {
    streamText = 'Reconnecting'
  }

  function onTz(tz: DisplayTimezone) {
    setDisplayTz(tz)
    saveTimezone(tz)
  }

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <header className="app-header">
      <div className="brand">
        <div className="brand-copy">
          <div className="brand-name">Zahnrad</div>
          <div className="brand-meta">
            <span>Model Blue</span>
            <span className="brand-dot" />
            <span className="paper-pill">Paper</span>
            {currentAccount ? (
              <>
                <span className="brand-dot" />
                <span className="mono bold" style={{ color: 'var(--blue)' }}>
                  {currentAccount}
                </span>
              </>
            ) : null}
          </div>
        </div>
        <AppNav />
      </div>
      <div className="header-status">
        {user && (
          <div className="user-badge" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginRight: '0.5rem' }}>
            <span className="mono" style={{ fontSize: '0.8rem', color: 'var(--text-bright)' }}>
              {user.email} <span style={{ opacity: 0.6 }}>({user.role})</span>
            </span>
            <button
              type="button"
              onClick={handleLogout}
              style={{
                padding: '0.2rem 0.5rem',
                fontSize: '0.75rem',
                background: 'rgba(239, 68, 68, 0.2)',
                color: '#fca5a5',
                border: '1px solid rgba(239, 68, 68, 0.4)',
                borderRadius: '4px',
                cursor: 'pointer',
              }}
            >
              Logout
            </button>
          </div>
        )}
        <span className={`dot ${streamClass}`} title="Position stream">
          <i />
          {streamText}
        </span>
        <span className="dot dim" title="Last stream or snapshot time">
          Updated{' '}
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
    </header>
  )
}
