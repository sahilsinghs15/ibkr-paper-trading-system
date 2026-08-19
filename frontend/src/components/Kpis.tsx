import { useMemo } from 'react'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import { fmtInt, fmtPnl, num, pnlClass } from '../utils/format'

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
  for (const legs of activeTrades.values()) {
    const head = legs[0]
    const uv = num(head.unrealized_pnl)
    if (uv !== null) {
      openPnl += uv
      openPnlAny = true
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

  return (
    <section className="kpis">
      <article className="kpi">
        <label>Open Positions</label>
        <div className="v">{fmtInt(activeTrades.size)}</div>
      </article>
      <article className="kpi">
        <label>Open P&amp;L</label>
        <div className={`v ${openPnlAny ? pnlClass(openPnl) : 'pnl-zero'}`}>
          {openPnlAny ? fmtPnl(openPnl) : '—'}
        </div>
      </article>
      <article className="kpi">
        <label>Realized P&amp;L</label>
        <div className={`v ${realizedPnlAny ? pnlClass(realizedPnl) : 'pnl-zero'}`}>
          {realizedPnlAny ? fmtPnl(realizedPnl) : '—'}
        </div>
      </article>
      <article className="kpi">
        <label>Active Trades</label>
        <div className="v">{fmtInt(activeTrades.size)}</div>
      </article>
    </section>
  )
}
