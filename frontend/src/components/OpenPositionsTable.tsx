import { Fragment } from 'react'
import { streamHint, timeColLabel } from './DashboardHeader'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import {
  badgeClass,
  blank,
  displayInstrument,
  fmtNum,
  fmtPnl,
  fmtTime,
  fmtUsd,
  markOf,
  pnlClass,
  statusLabel,
} from '../utils/format'

export function OpenPositionsTable() {
  const active = usePnlStore((s) => s.active)
  const streamState = usePnlStore((s) => s.streamState)
  const displayTz = usePnlStore((s) => s.displayTz)
  const trades = [...groupLegs(active).values()]

  return (
    <>
      <div className="section-h">
        <h2>POSITIONS</h2>
        <span className="muted">{streamHint(streamState)}</span>
      </div>
      <div className="board">
        <table>
          <thead>
            <tr>
              <th>STATUS</th>
              <th>{timeColLabel(displayTz)}</th>
              <th>TRADE ID</th>
              <th>ACCOUNT</th>
              <th>SYMBOL</th>
              <th>INSTRUMENT</th>
              <th>SIDE</th>
              <th className="right">QTY</th>
              <th className="right">ENTRY</th>
              <th className="right">MARK</th>
              <th className="right">UNREALIZED</th>
              <th className="right">REALIZED</th>
              <th>BASKET</th>
              <th>BROKER</th>
            </tr>
          </thead>
          <tbody>
            {!trades.length ? (
              <tr>
                <td colSpan={14} className="empty">
                  No open positions in snapshot/stream.
                </td>
              </tr>
            ) : (
              trades.map((legs) => {
                const head = legs[0]
                const st = statusLabel(head)
                const pair = legs
                  .map((x) => x.symbol)
                  .filter(Boolean)
                  .join(' / ')
                const tk = `${head.account_id}|${head.trade_id}`
                return (
                  <Fragment key={tk}>
                    <tr className="trade-head">
                      <td>
                        <span className={`badge ${badgeClass(st)}`}>{st}</span>
                      </td>
                      <td className="muted">
                        {fmtTime(
                          head.timestamp || head.fill_timestamp,
                          displayTz,
                        )}
                      </td>
                      <td className="tid" colSpan={2}>
                        {blank(head.trade_id)}
                        <div className="dim">
                          {blank(head.ibkr_account)} · {blank(head.strategy_id)}
                        </div>
                      </td>
                      <td colSpan={6} className="muted">
                        {pair}
                      </td>
                      <td className={`right ${pnlClass(head.unrealized_pnl)}`}>
                        {fmtPnl(head.unrealized_pnl)}
                      </td>
                      <td className={`right ${pnlClass(head.realized_pnl)}`}>
                        {fmtPnl(head.realized_pnl)}
                      </td>
                      <td colSpan={2} />
                    </tr>
                    {legs.map((row) => {
                      const m = markOf(row)
                      return (
                        <tr
                          className="leg"
                          key={`${row.account_id}|${row.trade_id}|${row.symbol}`}
                        >
                          <td />
                          <td className="dim">
                            {fmtTime(
                              row.fill_timestamp || row.timestamp,
                              displayTz,
                            )}
                          </td>
                          <td className="dim">{blank(row.broker_order_id)}</td>
                          <td className="dim">{blank(row.ibkr_account)}</td>
                          <td className="sym">{blank(row.symbol)}</td>
                          <td>{displayInstrument(row.instrument_type)}</td>
                          <td>{blank(row.side)}</td>
                          <td className="right">
                            {fmtNum(row.filled_quantity ?? row.quantity, 4)}
                          </td>
                          <td className="right">{fmtUsd(row.entry_price)}</td>
                          <td className="right">
                            {m === '—' ? '—' : fmtUsd(m)}
                          </td>
                          <td className="right dim">—</td>
                          <td className="right dim">—</td>
                          <td>
                            <span
                              className={`badge ${badgeClass(row.basket_state)}`}
                            >
                              {blank(row.basket_state)}
                            </span>
                          </td>
                          <td>
                            <span
                              className={`badge ${badgeClass(row.order_status)}`}
                            >
                              {blank(row.order_status)}
                            </span>
                          </td>
                        </tr>
                      )
                    })}
                  </Fragment>
                )
              })
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}
