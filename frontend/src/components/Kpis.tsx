import { groupLegs, usePnlStore } from '../store/pnlStore'
import { fmtPnl, num, pnlClass } from '../utils/format'

export function Kpis() {
  const active = usePnlStore((s) => s.active)
  const trades = groupLegs(active)
  let u = 0
  let r = 0
  let uAny = false
  let rAny = false
  for (const legs of trades.values()) {
    const row = legs[0]
    const uv = num(row.unrealized_pnl)
    const rv = num(row.realized_pnl)
    if (uv !== null) {
      u += uv
      uAny = true
    }
    if (rv !== null) {
      r += rv
      rAny = true
    }
  }

  return (
    <section className="kpis">
      <article className="kpi">
        <label>OPEN POSITIONS</label>
        <div className="v">{trades.size}</div>
      </article>
      <article className="kpi">
        <label>OPEN P&amp;L</label>
        <div className={`v ${uAny ? pnlClass(u) : 'pnl-zero'}`}>
          {uAny ? fmtPnl(u) : '—'}
        </div>
      </article>
      <article className="kpi">
        <label>REALIZED P&amp;L</label>
        <div className={`v ${rAny ? pnlClass(r) : 'pnl-zero'}`}>
          {rAny ? fmtPnl(r) : '—'}
        </div>
      </article>
      <article className="kpi">
        <label>ACTIVE TRADES</label>
        <div className="v">{trades.size}</div>
      </article>
    </section>
  )
}
