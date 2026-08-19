import { useMemo } from 'react'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import {
  blank,
  closeTimeColLabel,
  displayStrategy,
  fmtPnl,
  fmtTime,
  fmtUsd,
  pnlClass,
} from '../utils/format'

export function ClosedPositionsTable({ accountFilter }: { accountFilter?: string }) {
  const closed = usePnlStore((s) => s.closed)
  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()

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

  const closedTrades = [...groupLegs(filteredClosed).values()]

  return (
    <>
      <div className="section-h section-h-gap">
        <h2>Recently Closed</h2>
      </div>
      <div className="board">
        <table>
          <thead>
            <tr>
              <th>Trade ID</th>
              <th>Pair</th>
              <th>Account</th>
              <th>{closeTimeColLabel(displayTz)}</th>
              <th className="right">Realized P&amp;L</th>
              <th className="right">Commission</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {!closedTrades.length ? (
              <tr>
                <td colSpan={7} className="empty">
                  No closed trades.
                </td>
              </tr>
            ) : (
              closedTrades.map((legs) => {
                const head = legs[0]
                const pair = legs
                  .map((x) => x.symbol)
                  .filter(Boolean)
                  .join(' / ')
                const tk = `${head.account_id}|${head.trade_id}`
                return (
                  <tr className="leg" key={tk}>
                    <td className="tid">
                      {blank(head.trade_id)}
                      <div className="dim">{displayStrategy(head.strategy_id)}</div>
                    </td>
                    <td>{pair || '—'}</td>
                    <td>{blank(head.ibkr_account)}</td>
                    <td>
                      {fmtTime(
                        head.timestamp || head.fill_timestamp,
                        displayTz,
                      )}
                    </td>
                    <td className={`right ${pnlClass(head.realized_pnl)}`}>
                      {fmtPnl(head.realized_pnl)}
                    </td>
                    <td className="right">{fmtUsd(head.commission)}</td>
                    <td>
                      <span className="badge b-closed">CLOSED</span>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}
