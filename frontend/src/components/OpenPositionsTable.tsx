import { useMemo } from 'react'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import {
  badgeClass,
  blank,
  displayInstrument,
  displayStrategy,
  fmtPnl,
  fmtQty,
  fmtTime,
  fmtUsd,
  markOf,
  pnlClass,
  statusLabel,
  streamHint,
  timeColLabel,
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
    <>
      <div className="section-h">
        <h2>Positions</h2>
        <span className="muted">{streamHint(streamState)}</span>
      </div>
      <div className="board">
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>{timeColLabel(displayTz)}</th>
              <th>Trade ID</th>
              <th>Account</th>
              <th>Symbol</th>
              <th>Instrument</th>
              <th>Side</th>
              <th className="right">Qty</th>
              <th className="right">Entry</th>
              <th className="right">Mark</th>
              <th className="right">Unrealized</th>
              <th className="right">Realized</th>
              <th>Basket</th>
              <th>Broker</th>
            </tr>
          </thead>
          {trades.length === 0 ? (
            <tbody>
              <tr>
                <td colSpan={14} className="empty">
                  No open positions.
                </td>
              </tr>
            </tbody>
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
                <tbody className="trade-group" key={tk}>
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
                    <td className="trade-identity" colSpan={8}>
                      <div className="trade-strategy">
                        {displayStrategy(head.strategy_id)}
                      </div>
                      <div className="trade-pair">{pair || '—'}</div>
                      <div className="trade-meta">
                        <span>{blank(head.trade_id)}</span>
                        <span className="sep">·</span>
                        <span>{blank(head.ibkr_account)}</span>
                      </div>
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
                    const side = String(row.side || '').toUpperCase()
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
                        <td className={side === 'BUY' ? 'side-buy' : side === 'SELL' ? 'side-sell' : undefined}>
                          {blank(row.side)}
                        </td>
                        <td className="right">
                          {fmtQty(row.filled_quantity ?? row.quantity)}
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
                </tbody>
              )
            })
          )}
        </table>
      </div>
    </>
  )
}
