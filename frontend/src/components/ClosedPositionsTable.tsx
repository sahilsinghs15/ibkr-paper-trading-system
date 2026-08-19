import { closeTimeColLabel } from './DashboardHeader'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import {
  blank,
  fmtPnl,
  fmtTime,
  fmtUsd,
  pnlClass,
} from '../utils/format'

export function ClosedPositionsTable() {
  const closed = usePnlStore((s) => s.closed)
  const displayTz = usePnlStore((s) => s.displayTz)
  const closedTrades = [...groupLegs(closed).values()]

  return (
    <>
      <div className="section-h" style={{ marginTop: 14 }}>
        <h2>RECENTLY CLOSED</h2>
      </div>
      <div className="board">
        <table>
          <thead>
            <tr>
              <th>TRADE ID</th>
              <th>PAIR</th>
              <th>ACCOUNT</th>
              <th>{closeTimeColLabel(displayTz)}</th>
              <th className="right">REALIZED P&amp;L</th>
              <th className="right">COMMISSION</th>
              <th>STATUS</th>
            </tr>
          </thead>
          <tbody>
            {!closedTrades.length ? (
              <tr>
                <td colSpan={7} className="empty">
                  No closed events this session.
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
                    <td className="tid">{blank(head.trade_id)}</td>
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
