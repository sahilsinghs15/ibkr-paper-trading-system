import { useMemo } from 'react'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import { fmtCompactCurrency, fmtPnl, num, pnlClass } from '../utils/format'

export function Kpis({ accountFilter }: { accountFilter?: string }) {
  const active = usePnlStore((s) => s.active)
  const closed = usePnlStore((s) => s.closed)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()

  const filteredActive = useMemo(() => {
    if (!cleanFilter) return active
    const out: typeof active = {}
    for (const [k, v] of Object.entries(active)) {
      if (String(v.ibkr_account || '').trim().toUpperCase() === cleanFilter) {
        out[k] = v
      }
    }
    return out
  }, [active, cleanFilter])

  const filteredClosed = useMemo(() => {
    if (!cleanFilter) return closed
    const out: typeof closed = {}
    for (const [k, v] of Object.entries(closed)) {
      if (String(v.ibkr_account || '').trim().toUpperCase() === cleanFilter) {
        out[k] = v
      }
    }
    return out
  }, [closed, cleanFilter])

  const activeTrades = groupLegs(filteredActive)
  const closedTrades = groupLegs(filteredClosed)

  let openPnl = 0
  let openPnlAny = false
  let grossMarketValue = 0
  let longValue = 0
  let shortValue = 0

  for (const legs of activeTrades.values()) {
    const head = legs[0]
    const uv = num(head.unrealized_pnl)
    if (uv !== null) {
      openPnl += uv
      openPnlAny = true
    }
    for (const leg of legs) {
      const q = Math.abs(num(leg.filled_quantity ?? leg.quantity) || 0)
      const p = num(leg.mark_price || leg.entry_price || leg.last_price) || 0
      const notional = q * p
      grossMarketValue += notional
      const side = String(leg.side || '').toUpperCase()
      if (side === 'BUY') {
        longValue += notional
      } else if (side === 'SELL') {
        shortValue += notional
      }
    }
  }

  let realizedPnl = 0
  let realizedPnlAny = false
  for (const legs of closedTrades.values()) {
    const head = legs[0]
    const rv = num(head.realized_pnl)
    if (rv !== null) {
      realizedPnl += rv
      realizedPnlAny = true
    }
  }

  const totalExposure = longValue + shortValue || 1
  const longPct = Math.round((longValue / totalExposure) * 100)
  const shortPct = Math.round((shortValue / totalExposure) * 100)
  const hedgeDiff = shortPct - longPct
  const hedgeText = hedgeDiff >= 0 ? `+${hedgeDiff}% short` : `+${Math.abs(hedgeDiff)}% long`

  const totalR = openPnlAny ? (openPnl / 1000).toFixed(2) : '0.00'
  const avgR = activeTrades.size > 0 ? (Number(totalR) / activeTrades.size).toFixed(2) : '0.00'

  return (
    <section className="kpis factory-kpis">
      {/* Card 1: MARKET VALUE */}
      <article className="kpi factory-kpi">
        <label>MARKET VALUE</label>
        <div className="v">{fmtCompactCurrency(grossMarketValue)}</div>
        <div className="kpi-accent-bar blue" />
      </article>

      {/* Card 2: OPEN PNL */}
      <article className="kpi factory-kpi">
        <label>OPEN PNL</label>
        <div className={`v ${openPnlAny ? pnlClass(openPnl) : 'pnl-zero'}`}>
          {openPnlAny ? fmtPnl(openPnl) : '$0.00'}
        </div>
        <div className="kpi-subtext dim">
          {activeTrades.size > 0 ? `Across ${activeTrades.size} active pair(s)` : 'No active trades'}
        </div>
      </article>

      {/* Card 3: NET LONG -- SHORT HEDGE */}
      <article className="kpi factory-kpi hedge-kpi">
        <div className="hedge-h">
          <label>NET LONG — SHORT · HEDGE</label>
          <span className={`badge-pill ${hedgeDiff >= 0 ? 'red' : 'green'}`}>{hedgeText}</span>
        </div>
        <div className="hedge-bars">
          <div className="hedge-bar-row">
            <span className="lbl">LONG</span>
            <div className="bar-track">
              <div className="bar-fill long" style={{ width: `${longPct}%` }} />
            </div>
            <span className="val">{fmtCompactCurrency(longValue)}</span>
          </div>
          <div className="hedge-bar-row">
            <span className="lbl">SHORT</span>
            <div className="bar-track">
              <div className="bar-fill short" style={{ width: `${shortPct}%` }} />
            </div>
            <span className="val">{fmtCompactCurrency(shortValue)}</span>
          </div>
        </div>
      </article>

      {/* Card 4: TOTAL R */}
      <article className="kpi factory-kpi">
        <div className="kpi-h">
          <label>TOTAL R</label>
          <span className="sub-lbl">REALIZED {realizedPnlAny ? fmtPnl(realizedPnl) : '$0.00'}</span>
        </div>
        <div className={`v ${openPnl >= 0 ? 'pnl-pos' : 'pnl-neg'}`}>{totalR}R</div>
        <div className="kpi-accent-bar red" />
        <div className="kpi-subtext dim">avg {avgR}R / trade</div>
      </article>
    </section>
  )
}
