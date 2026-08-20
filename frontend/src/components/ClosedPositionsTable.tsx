import { useMemo, useState } from 'react'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import {
  calcAgeDays,
  calcRMultiple,
  fmtCompactCurrency,
  fmtFactoryDate,
  fmtPnl,
  num,
  pnlClass,
} from '../utils/format'

function getEpochMs(isoStr?: string | null): number {
  if (!isoStr) return 0
  const ms = new Date(isoStr).getTime()
  return isNaN(ms) ? 0 : ms
}

export function ClosedPositionsTable({ accountFilter }: { accountFilter?: string }) {
  const closed = usePnlStore((s) => s.closed)
  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()
  const [historyOrder, setHistoryOrder] = useState<'RECENT' | 'OLDER'>('RECENT')

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

  const closedTrades = useMemo(() => {
    const list = [...groupLegs(filteredClosed).values()]

    // Deterministically sort by CLOSED TIME
    list.sort((a, b) => {
      const headA = a[0]
      const headB = b[0]
      const closeTsA = getEpochMs(headA.closed_at || headA.fill_timestamp || headA.timestamp)
      const closeTsB = getEpochMs(headB.closed_at || headB.fill_timestamp || headB.timestamp)

      if (historyOrder === 'RECENT') {
        return closeTsB - closeTsA // Newest closed trades first (DESC)
      }
      return closeTsA - closeTsB // Older trades chronologically (ASC)
    })

    return list
  }, [filteredClosed, historyOrder])

  return (
    <section className="factory-panel-section">
      <div className="section-h factory-h">
        <div className="factory-title-block">
          <h2>FACTORY PANEL — RECENTLY CLOSED POSITIONS ({closedTrades.length})</h2>
          <span className="factory-subtitle">MODEL BLUE X-SERIES · HISTORICAL TRADES</span>
        </div>

        {/* Recent vs Older Filter Toggle */}
        <div className="history-filter-toggle">
          <button
            type="button"
            className={`history-filter-btn ${historyOrder === 'RECENT' ? 'active' : ''}`}
            onClick={() => setHistoryOrder('RECENT')}
            title="Show most recent closed trades first (Closed Time DESC)"
          >
            RECENT (NEWEST FIRST)
          </button>
          <button
            type="button"
            className={`history-filter-btn ${historyOrder === 'OLDER' ? 'active' : ''}`}
            onClick={() => setHistoryOrder('OLDER')}
            title="Show older historical trades chronologically (Closed Time ASC)"
          >
            OLDER (CHRONOLOGICAL)
          </button>
        </div>
      </div>

      <div className="board factory-board scrollable-table-container">
        <table className="factory-table">
          <thead>
            <tr>
              <th style={{ width: '4%' }}>SNO</th>
              <th style={{ width: '13%' }}>ENTRY</th>
              <th style={{ width: '13%' }}>CLOSED TIME</th>
              <th style={{ width: '6%' }}>AGE</th>
              <th style={{ width: '14%' }}>PAIR</th>
              <th style={{ width: '30%' }}>EXPOSURE BALANCE</th>
              <th style={{ width: '10%', textAlign: 'right' }}>REALIZED PL</th>
              <th style={{ width: '10%', textAlign: 'right' }}>PROGRESS</th>
            </tr>
          </thead>
          <tbody>
            {!closedTrades.length ? (
              <tr>
                <td colSpan={8} className="empty">
                  No closed positions found.
                </td>
              </tr>
            ) : (
              closedTrades.map((legs, idx) => {
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

                const openTsRaw = head.opened_at || head.timestamp
                const closeTsRaw = head.closed_at || head.fill_timestamp

                const age = calcAgeDays(closeTsRaw || openTsRaw)
                const entryStr = openTsRaw ? fmtFactoryDate(openTsRaw, displayTz) : '—'
                const closedStr = closeTsRaw ? fmtFactoryDate(closeTsRaw, displayTz) : '—'
                const r = calcRMultiple(head.realized_pnl, totalNotional)
                const tk = `${head.account_id}|${head.trade_id}`
                const rowSno = idx + 1

                return (
                  <tr key={tk} className="factory-row">
                    {/* 1. SNO */}
                    <td className="mono dim sno">{rowSno}</td>

                    {/* 2. ENTRY */}
                    <td className="mono entry-cell">{entryStr}</td>

                    {/* 3. CLOSED TIME */}
                    <td className="mono entry-cell closed-cell">{closedStr}</td>

                    {/* 4. AGE */}
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

                    {/* 5. PAIR */}
                    <td className="pair-cell">
                      <div className="pair-badges">
                        <span className="badge-pair leg-a">{legA.symbol || '—'}</span>
                        <span className="badge-pair leg-b">{legB.symbol || '—'}</span>
                      </div>
                    </td>

                    {/* 6. EXPOSURE BALANCE */}
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

                    {/* 7. REALIZED PL */}
                    <td className={`right pl-cell ${pnlClass(head.realized_pnl)}`}>
                      {fmtPnl(head.realized_pnl)}
                    </td>

                    {/* 8. PROGRESS */}
                    <td className="right progress-cell">
                      <div className="r-pill-box">
                        <span className={`r-pill ${r.isPos ? 'pos' : 'neg'}`}>{r.text}</span>
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
