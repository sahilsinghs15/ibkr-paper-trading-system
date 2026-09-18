import { useEffect, useMemo, useState } from 'react'
import type { ManualPositionApiRow } from '../../api/manualTradingApi'
import { usePnlStore } from '../../store/pnlStore'
import { fmtPnl, fmtQty, fmtTime, fmtUsd, pnlClass, tzShortLabel } from '../../utils/format'
import { Pagination } from '../Pagination'
import type { LiveManualMark } from './useManualWorkspace'

const PAGE_SIZE = 10

interface Props {
  account: string
  rows: ManualPositionApiRow[]
  loading: boolean
  loaded: boolean
  error: string | null
  liveByTrade: Map<string, LiveManualMark>
  ledgerOpen: boolean
  onToggleLedger: () => void
  onRetry: () => void
  onClose: (row: ManualPositionApiRow, closeSide: 'BUY' | 'SELL', absQty: number) => void
}

interface ViewRow {
  row: ManualPositionApiRow
  qty: number
  absQty: number
  side: 'BUY' | 'SELL'
  avg: number
  mark: number | null
  marketValue: number | null
  unrealized: number | null
  realized: number
  marketStatus: string | null
}

function toView(row: ManualPositionApiRow, live: LiveManualMark | undefined): ViewRow {
  const qty = Number(row.signed_qty)
  const avg = Number(row.avg_cost)
  const mark = live?.mark ?? null
  const unrealized = mark != null ? (mark - avg) * qty : (live?.unrealized ?? null)
  return {
    row,
    qty,
    absQty: Math.abs(qty),
    side: qty >= 0 ? 'BUY' : 'SELL',
    avg,
    mark,
    marketValue: mark != null ? Math.abs(qty) * mark : null,
    unrealized,
    realized: Number(row.realized_pnl),
    marketStatus: live?.marketStatus ?? null,
  }
}

function signedUsd(v: number): string {
  return v < 0 ? `-${fmtUsd(Math.abs(v))}` : fmtUsd(v)
}

function Tile({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="mw-tile">
      <span>{label}</span>
      <strong className={cls}>{value}</strong>
    </div>
  )
}

export function ManualPositionsPanel({
  account,
  rows,
  loading,
  loaded,
  error,
  liveByTrade,
  ledgerOpen,
  onToggleLedger,
  onRetry,
  onClose,
}: Props) {
  const displayTz = usePnlStore((s) => s.displayTz)
  const [page, setPage] = useState(1)

  const view = useMemo(
    () => rows.map((r) => toView(r, liveByTrade.get(r.trade_id))),
    [rows, liveByTrade],
  )

  const summary = useMemo(() => {
    let gross = 0
    let net = 0
    let unreal = 0
    let realized = 0
    let marked = 0
    for (const v of view) {
      if (v.mark != null) {
        gross += v.absQty * v.mark
        net += v.qty * v.mark
        marked += 1
      }
      if (v.unrealized != null) unreal += v.unrealized
      if (Number.isFinite(v.realized)) realized += v.realized
    }
    return { gross, net, unreal, realized, marked }
  }, [view])

  useEffect(() => setPage(1), [account])
  useEffect(() => {
    const totalPages = Math.max(1, Math.ceil(view.length / PAGE_SIZE))
    if (page > totalPages) setPage(totalPages)
  }, [view.length, page])

  const visible = ledgerOpen ? view : view.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
  const allMarked = view.length > 0 && summary.marked === view.length
  const noMarks = view.length > 0 && summary.marked === 0
  const colCount = ledgerOpen ? 16 : 10

  return (
    <section className="mw-section" aria-labelledby="mw-positions-title">
      <header className="mw-section-head">
        <div className="mw-section-title">
          <h2 id="mw-positions-title">Manual Positions</h2>
          <span className="mw-count">{loaded ? `${view.length} open` : '—'}</span>
          <span className="mw-hint">Current manual exposure · live marks · engine positions excluded</span>
        </div>
        <div className="mw-section-actions">
          {loading && loaded && <span className="mw-hint">Updating…</span>}
          <button
            type="button"
            className={`manual-btn ${ledgerOpen ? 'manual-btn-primary' : ''}`}
            onClick={onToggleLedger}
            aria-expanded={ledgerOpen}
            aria-controls="mw-positions-table"
            disabled={!loaded || view.length === 0}
          >
            {ledgerOpen ? 'Collapse ledger ▴' : 'Full ledger ▾'}
          </button>
        </div>
      </header>

      <div className="mw-tiles">
        <Tile label="Open positions" value={loaded ? String(view.length) : '—'} />
        <Tile label="Gross market value" value={summary.marked ? fmtUsd(summary.gross) : '—'} />
        <Tile label="Net market value" value={summary.marked ? signedUsd(summary.net) : '—'} />
        <Tile
          label="Unrealized P&L"
          value={noMarks ? 'Mark unavailable' : view.length ? fmtPnl(summary.unreal) : '—'}
          cls={noMarks || !view.length ? '' : pnlClass(summary.unreal)}
        />
        <Tile
          label="Realized P&L (open lots)"
          value={view.length ? fmtPnl(summary.realized) : '—'}
          cls={view.length ? pnlClass(summary.realized) : ''}
        />
      </div>
      {view.length > 0 && !allMarked && !noMarks && (
        <div className="mw-note warn">
          Live mark unavailable for {view.length - summary.marked} position(s); their value and unrealized P&L are excluded
          from the totals.
        </div>
      )}

      {error && (
        <div className="mw-note error">
          {loaded ? 'Showing last loaded positions — refresh failed: ' : ''}
          {error}{' '}
          <button type="button" className="manual-btn" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}

      {ledgerOpen && (
        <div className="mw-ledger-banner">
          <strong>Full ledger</strong> · every open manual lot with identifiers, contract and timestamps ({view.length}{' '}
          row{view.length === 1 ? '' : 's'}) · times in {tzShortLabel(displayTz)}
        </div>
      )}

      <div className={`manual-table-wrap mw-table-wrap ${ledgerOpen ? 'is-ledger' : ''}`} id="mw-positions-table">
        <table className="manual-table mw-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th className="num">Qty</th>
              <th className="num">Avg cost</th>
              <th className="num">Mark</th>
              <th className="num">Market value</th>
              <th className="num">Unrealized P&amp;L</th>
              <th className="num">Realized P&amp;L</th>
              {ledgerOpen && (
                <>
                  <th>Trade ID</th>
                  <th className="num">conId</th>
                  <th>Ccy</th>
                  <th>Opened</th>
                  <th>Last update</th>
                  <th>Market data</th>
                </>
              )}
              <th>Status</th>
              <th className="num">Action</th>
            </tr>
          </thead>
          <tbody>
            {!loaded ? (
              <tr>
                <td colSpan={colCount} className="mw-empty">
                  {error ? 'Positions could not be loaded.' : 'Loading manual positions…'}
                </td>
              </tr>
            ) : view.length === 0 ? (
              <tr>
                <td colSpan={colCount} className="mw-empty">
                  No open manual positions for {account || '—'}. Positions appear here once a manual order fills.
                </td>
              </tr>
            ) : (
              visible.map((v) => (
                <tr key={v.row.id}>
                  <td className="mono strong">{v.row.symbol}</td>
                  <td>
                    <span className={v.side === 'BUY' ? 'side-buy' : 'side-sell'}>{v.side}</span>
                  </td>
                  <td className="mono num">{fmtQty(v.absQty)}</td>
                  <td className="mono num">{fmtUsd(v.avg)}</td>
                  <td className="mono num">{v.mark != null ? fmtUsd(v.mark) : <span className="dim">Unavailable</span>}</td>
                  <td className="mono num">{v.marketValue != null ? fmtUsd(v.marketValue) : '—'}</td>
                  <td className={`mono num ${v.unrealized != null ? pnlClass(v.unrealized) : ''}`}>
                    {v.unrealized != null ? fmtPnl(v.unrealized) : <span className="dim">—</span>}
                  </td>
                  <td className={`mono num ${pnlClass(v.realized)}`}>{fmtPnl(v.realized)}</td>
                  {ledgerOpen && (
                    <>
                      <td className="mono" title={v.row.trade_id}>
                        {v.row.trade_id}
                      </td>
                      <td className="mono num">{v.row.con_id}</td>
                      <td>{v.row.currency}</td>
                      <td className="mono">{fmtTime(v.row.opened_at, displayTz)}</td>
                      <td className="mono">{fmtTime(v.row.updated_at, displayTz)}</td>
                      <td>{v.marketStatus ?? <span className="dim">No live data</span>}</td>
                    </>
                  )}
                  <td>
                    <span className="badge b-exec">{v.row.status}</span>
                  </td>
                  <td className="num">
                    <button
                      type="button"
                      className="manual-btn manual-btn-danger mw-row-btn"
                      title={`Prepare a ${v.side === 'BUY' ? 'SELL' : 'BUY'} MARKET order for ${fmtQty(v.absQty)} ${v.row.symbol} in the order ticket`}
                      onClick={() => onClose(v.row, v.side === 'BUY' ? 'SELL' : 'BUY', v.absQty)}
                    >
                      Close
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {!ledgerOpen && view.length > PAGE_SIZE && (
        <div className="mw-footer">
          <Pagination currentPage={page} totalItems={view.length} pageSize={PAGE_SIZE} onPageChange={setPage} />
        </div>
      )}
    </section>
  )
}
