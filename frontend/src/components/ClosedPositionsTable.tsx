import { useCallback, useMemo, useState } from 'react'
import { SortableTh } from './SortableTh'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import type { PositionLeg } from '../types/position'
import {
  calcAgeDays,
  calcRMultiple,
  fmtCompactCurrency,
  fmtFactoryDate,
  fmtPnl,
  num,
  pnlClass,
} from '../utils/format'
import { sortRows, useTableSortState } from '../utils/tableSort'

function getEpochMs(isoStr?: string | null): number {
  if (!isoStr) return 0
  const ms = new Date(isoStr).getTime()
  return isNaN(ms) ? 0 : ms
}

function computeTradeTotalNotional(legs: PositionLeg[]): number {
  const legA = legs[0]
  const legB = legs[1] || legs[0]
  const legAQty = Math.abs(num(legA.filled_quantity ?? legA.quantity) || 0)
  const legAPrice = num(legA.mark_price || legA.entry_price || legA.last_price) || 0
  const legANotional = legAQty * legAPrice
  const legBQty = Math.abs(num(legB.filled_quantity ?? legB.quantity) || 0)
  const legBPrice = num(legB.mark_price || legB.entry_price || legB.last_price) || 0
  const legBNotional = legBQty * legBPrice
  return legANotional + legBNotional || 1
}

const CLOSED_POSITIONS_SORT_EXTRACTORS: Record<string, (legs: PositionLeg[]) => unknown> = {
  entry: (legs) => {
    const head = legs[0]
    return getEpochMs(head.opened_at || head.timestamp)
  },
  closed: (legs) => {
    const head = legs[0]
    return getEpochMs(head.closed_at || head.fill_timestamp || head.timestamp)
  },
  age: (legs) => {
    const head = legs[0]
    const openMs = getEpochMs(head.opened_at || head.timestamp)
    const closeMs = getEpochMs(head.closed_at || head.fill_timestamp || head.timestamp)
    if (closeMs > 0 && openMs > 0) return closeMs - openMs
    return closeMs || openMs || null
  },
  pair: (legs) => {
    const legA = legs[0]
    const legB = legs[1] || legs[0]
    return `${legA.symbol || ''}/${legB.symbol || ''}`
  },
  exposure: (legs) => {
    return computeTradeTotalNotional(legs)
  },
  pl: (legs) => {
    const head = legs[0]
    return head.realized_pnl !== null && head.realized_pnl !== undefined
      ? num(head.realized_pnl)
      : null
  },
  progress: (legs) => {
    const head = legs[0]
    const notional = computeTradeTotalNotional(legs)
    return head.realized_pnl !== null && head.realized_pnl !== undefined
      ? (num(head.realized_pnl) || 0) / notional
      : null
  },
}

export function ClosedPositionsTable({ accountFilter }: { accountFilter?: string }) {
  const closed = usePnlStore((s) => s.closed)
  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()
  const [historyOrder, setHistoryOrder] = useState<'RECENT' | 'OLDER'>('RECENT')
  const [downloadingCsv, setDownloadingCsv] = useState(false)
  const { sortKey, sortDir, handleSort } = useTableSortState()

  const handleDownloadCsv = useCallback(async () => {
    try {
      setDownloadingCsv(true)
      const params = new URLSearchParams()
      if (cleanFilter) {
        params.append('ibkr_account', cleanFilter)
      }
      const url = `/demo/closed-positions/csv${params.toString() ? `?${params.toString()}` : ''}`
      const token = localStorage.getItem('ibkr_trading_jwt_token')
      const response = await fetch(url, {
        method: 'GET',
        headers: {
          Accept: 'text/csv',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
      })
      if (!response.ok) {
        throw new Error(`CSV export failed: ${response.statusText}`)
      }
      const blob = await response.blob()
      let filename = 'closed_trades.csv'
      const disposition = response.headers.get('Content-Disposition')
      if (disposition && disposition.includes('filename=')) {
        const match = disposition.match(/filename="?([^"]+)"?/)
        if (match && match[1]) {
          filename = match[1]
        }
      }
      const downloadUrl = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = downloadUrl
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      window.URL.revokeObjectURL(downloadUrl)
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : 'Failed to download CSV')
    } finally {
      setDownloadingCsv(false)
    }
  }, [cleanFilter])

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

  const defaultSort = useCallback(
    (a: PositionLeg[], b: PositionLeg[]) => {
      const headA = a[0]
      const headB = b[0]
      const closeTsA = getEpochMs(headA.closed_at || headA.fill_timestamp || headA.timestamp)
      const closeTsB = getEpochMs(headB.closed_at || headB.fill_timestamp || headB.timestamp)

      if (historyOrder === 'RECENT') {
        return closeTsB - closeTsA // Newest closed trades first (DESC)
      }
      return closeTsA - closeTsB // Older trades chronologically (ASC)
    },
    [historyOrder],
  )

  const closedTrades = useMemo(() => {
    const list = [...groupLegs(filteredClosed).values()]
    return sortRows(list, sortKey, sortDir, CLOSED_POSITIONS_SORT_EXTRACTORS, defaultSort)
  }, [filteredClosed, sortKey, sortDir, defaultSort])

  return (
    <section className="factory-panel-section">
      <div className="section-h factory-h">
        <div className="factory-title-block">
          <h2>FACTORY PANEL — RECENTLY CLOSED POSITIONS ({closedTrades.length})</h2>
          <span className="factory-subtitle">MODEL BLUE X-SERIES · HISTORICAL TRADES</span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <button
            type="button"
            className="history-filter-btn"
            onClick={handleDownloadCsv}
            disabled={downloadingCsv}
            title="Download full closed trades dataset as CSV (all matching records)"
            style={{ color: 'var(--ink)', fontWeight: 600 }}
          >
            {downloadingCsv ? 'Downloading…' : '⬇ Download CSV'}
          </button>

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
      </div>

      <div className="board factory-board scrollable-table-container">
        <table className="factory-table">
          <thead>
            <tr>
              <th style={{ width: '4%' }}>SNO</th>
              <SortableTh sortKey="entry" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '12%' }}>ENTRY</SortableTh>
              <SortableTh sortKey="closed" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '12%' }}>CLOSED TIME</SortableTh>
              <SortableTh sortKey="age" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '6%' }}>AGE</SortableTh>
              <SortableTh sortKey="pair" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '11%' }}>PAIR</SortableTh>
              <SortableTh sortKey="exposure" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '35%' }}>EXPOSURE BALANCE</SortableTh>
              <SortableTh sortKey="pl" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} align="right" style={{ width: '10%', textAlign: 'right' }}>REALIZED PL</SortableTh>
              <SortableTh sortKey="progress" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} align="right" style={{ width: '10%', textAlign: 'right' }}>PROGRESS</SortableTh>
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
