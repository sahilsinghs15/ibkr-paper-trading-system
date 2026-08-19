import { useMemo } from 'react'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import {
  calcAgeDays,
  calcRMultiple,
  fmtCompactCurrency,
  fmtFactoryDate,
  fmtPnl,
  num,
  pnlClass,
  streamHint,
} from '../utils/format'

export function OpenPositionsTable({ accountFilter }: { accountFilter?: string }) {
  const active = usePnlStore((s) => s.active)
  const streamState = usePnlStore((s) => s.streamState)
  const displayTz = usePnlStore((s) => s.displayTz)
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

  const trades = [...groupLegs(filteredActive).values()]

  return (
    <section className="factory-panel-section">
      <div className="section-h factory-h">
        <div className="factory-title-block">
          <h2>FACTORY PANEL — OPEN POSITIONS</h2>
          <span className="factory-subtitle">MODEL BLUE X-SERIES · V1.1</span>
        </div>
        <span className="muted">{streamHint(streamState)}</span>
      </div>

      <div className="board factory-board">
        <table className="factory-table">
          <thead>
            <tr>
              <th style={{ width: '4%' }}>SNO</th>
              <th style={{ width: '14%' }}>ENTRY</th>
              <th style={{ width: '7%' }}>AGE</th>
              <th style={{ width: '15%' }}>PAIR</th>
              <th style={{ width: '36%' }}>EXPOSURE BALANCE</th>
              <th style={{ width: '12%', textAlign: 'right' }}>PL</th>
              <th style={{ width: '12%', textAlign: 'right' }}>PROGRESS</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td colSpan={7} className="empty">
                  No active positions open.
                </td>
              </tr>
            ) : (
              trades.map((legs, idx) => {
                const head = legs[0]
                const legA = legs[0]
                const legB = legs[1] || legs[0]

                const legAQty = Math.abs(num(legA.filled_quantity ?? legA.quantity) || 0)
                const legAPrice = num(legA.mark_price || legA.entry_price || legA.last_price) || 0
                const legANotional = legAQty * legAPrice

                const legBQty = Math.abs(num(legB.filled_quantity ?? legB.quantity) || 0)
                const legBPrice = num(legB.mark_price || legB.entry_price || legB.last_price) || 0
                const legBNotional = legBQty * legBPrice

                const totalNotional = legANotional + legBNotional || 1
                const legAPct = Math.round((legANotional / totalNotional) * 100)
                const legBPct = Math.round((legBNotional / totalNotional) * 100)
                const imbalance = Math.abs(legBPct - legAPct)
                const imbalanceSide = legBPct >= legAPct ? 'short' : 'long'
                const imbalanceText = `+${imbalance}% more ${imbalanceSide}`

                const age = calcAgeDays(head.timestamp || head.fill_timestamp)
                const entryStr = fmtFactoryDate(head.timestamp || head.fill_timestamp, displayTz)
                const r = calcRMultiple(head.unrealized_pnl, totalNotional)
                const tk = `${head.account_id}|${head.trade_id}`

                return (
                  <tr key={tk} className="factory-row">
                    {/* 1. SNO */}
                    <td className="mono dim sno">{idx + 1}</td>

                    {/* 2. ENTRY */}
                    <td className="mono entry-cell">{entryStr}</td>

                    {/* 3. AGE */}
                    <td className="age-cell">
                      <div className="age-wrapper">
                        <div className="age-bars">
                          {[0, 1, 2, 3, 4].map((i) => (
                            <span
                              key={i}
                              className={`age-bar ${i <= age.days ? 'active' : ''}`}
                            />
                          ))}
                        </div>
                        <span className="mono age-txt">{age.text}</span>
                      </div>
                    </td>

                    {/* 4. PAIR */}
                    <td className="pair-cell">
                      <div className="pair-badges">
                        <span className="badge-pair leg-a">{legA.symbol || '—'}</span>
                        <span className="badge-pair leg-b">{legB.symbol || '—'}</span>
                      </div>
                    </td>

                    {/* 5. EXPOSURE BALANCE */}
                    <td className="exposure-cell">
                      <div className="exposure-box">
                        <div className="exp-legs">
                          <div className="exp-leg leg-a">
                            <span className="sym">{legA.symbol}</span>
                            <div className="track">
                              <div className="fill" style={{ width: `${Math.max(15, legAPct)}%` }} />
                            </div>
                            <span className="val">{fmtCompactCurrency(legANotional)}</span>
                          </div>
                          <div className="exp-leg leg-b">
                            <span className="sym">{legB.symbol}</span>
                            <div className="track">
                              <div className="fill" style={{ width: `${Math.max(15, legBPct)}%` }} />
                            </div>
                            <span className="val">{fmtCompactCurrency(legBNotional)}</span>
                          </div>
                        </div>
                        <span className="imbalance-pill">{imbalanceText}</span>
                      </div>
                    </td>

                    {/* 6. PL */}
                    <td className={`right pl-cell ${pnlClass(head.unrealized_pnl)}`}>
                      {fmtPnl(head.unrealized_pnl)}
                    </td>

                    {/* 7. PROGRESS */}
                    <td className="right progress-cell">
                      <div className="progress-wrapper">
                        <div className="progress-track">
                          <div
                            className={`progress-fill ${r.isPos ? 'pos' : 'neg'}`}
                            style={{ width: `${Math.min(100, Math.max(10, Math.abs(r.r) * 50))}%` }}
                          />
                        </div>
                        <span className={`mono progress-txt ${r.isPos ? 'pnl-pos' : 'pnl-neg'}`}>
                          {r.text}
                        </span>
                      </div>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
