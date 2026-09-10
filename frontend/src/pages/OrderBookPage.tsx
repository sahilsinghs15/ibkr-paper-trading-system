import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchOrderBook } from '../api/brokerExecutionsApi'
import { SortableTh } from '../components/SortableTh'
import type { OrderBookResponse, OrderBookRow } from '../types/brokerExecution'
import { normalizeIbkrAccount } from '../utils/activeAccount'
import { fmtQty, fmtTime, fmtUsd, loadTimezone } from '../utils/format'
import { sortRows, useTableSortState } from '../utils/tableSort'

const SORT_EXTRACTORS: Record<string, (r: OrderBookRow) => unknown> = {
  time: (r) => new Date(r.created_at || '').getTime(),
  order_id: (r) => r.internal_order_id,
  broker_order_id: (r) => r.broker_order_id,
  symbol: (r) => r.symbol,
  side: (r) => r.side,
  qty: (r) => r.quantity,
  filled: (r) => r.filled,
  remaining: (r) => r.remaining,
  type: (r) => r.order_type,
  limit_price: (r) => r.limit_price,
  status: (r) => r.status,
  avg_fill: (r) => r.avg_fill_price,
  updated: (r) => new Date(r.updated_at || '').getTime(),
}

function statusClass(s: string): string {
  if (s === 'FILLED') return 'buy'
  if (s === 'REJECTED' || s === 'ERROR') return 'sell'
  if (s === 'CANCELLED') return 'muted'
  return ''
}

export function OrderBookPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount)
  const [data, setData] = useState<OrderBookResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState('')
  const [symbolFilter, setSymbolFilter] = useState('')
  const { sortKey, sortDir, handleSort } = useTableSortState()
  const tz = loadTimezone()
  const pageSize = 50

  useEffect(() => {
    if (!cleanAccount) return
    setLoading(true)
    setError(null)
    fetchOrderBook(cleanAccount, { page, page_size: pageSize, status: statusFilter || undefined, symbol: symbolFilter || undefined })
      .then((res) => {
        setData(res)
        setLoading(false)
      })
      .catch((err: unknown) => {
        const ae = err as { response?: { data?: { detail?: string } } }
        setError(ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Failed'))
        setLoading(false)
      })
  }, [cleanAccount, page, statusFilter, symbolFilter])

  const sorted = useMemo(() => {
    const list = data?.orders ?? []
    return sortRows(list, sortKey, sortDir, SORT_EXTRACTORS, (a, b) => {
      const ta = new Date(a.updated_at || '').getTime()
      const tb = new Date(b.updated_at || '').getTime()
      return tb - ta
    })
  }, [data?.orders, sortKey, sortDir])

  if (loading && !data) return <main className="page"><div className="trade-book-loading">Loading Order Book…</div></main>
  if (error && !data) return <main className="page"><div className="status-badge off">{error}</div></main>

  const totalPages = data ? Math.ceil(data.total / pageSize) : 1
  return (
    <main className="page trade-book-page">
      <header className="trade-book-header">
        <div className="trade-book-title-block">
          <h1>Order Book</h1>
          <span className="trade-book-subtitle">Order Lifecycle · {cleanAccount}</span>
        </div>
        <div className="trade-book-meta">
          <select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1) }}>
            <option value="">All status</option>
            {['PENDING','SUBMITTED','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED','ERROR'].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <input placeholder="Symbol" value={symbolFilter} onChange={(e) => { setSymbolFilter(e.target.value.toUpperCase()); setPage(1) }} style={{ width: 90 }} />
        </div>
      </header>
      <section className="trade-book-panel">
        <div className="trade-book-panel-head"><h2>Orders ({data?.total ?? 0})</h2><span className="trade-book-panel-hint">Durable orders · {data?.orders.length ?? 0} on page {page}/{totalPages}</span></div>
        <div className="trade-book-table-wrap">
          <table className="trade-book-table">
            <thead><tr>
              <SortableTh sortKey="time" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Time</SortableTh>
              <SortableTh sortKey="order_id" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Order ID</SortableTh>
              <SortableTh sortKey="broker_order_id" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Broker Order ID</SortableTh>
              <SortableTh sortKey="symbol" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Symbol</SortableTh>
              <SortableTh sortKey="side" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Side</SortableTh>
              <SortableTh sortKey="qty" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Qty</SortableTh>
              <SortableTh sortKey="filled" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Filled</SortableTh>
              <SortableTh sortKey="remaining" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Remaining</SortableTh>
              <SortableTh sortKey="type" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Type</SortableTh>
              <SortableTh sortKey="limit_price" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Limit</SortableTh>
              <SortableTh sortKey="status" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Status</SortableTh>
              <SortableTh sortKey="avg_fill" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Avg Fill</SortableTh>
              <SortableTh sortKey="updated" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Updated</SortableTh>
            </tr></thead>
            <tbody>
              {sorted.length === 0 ? <tr><td colSpan={13} className="trade-book-empty">No orders</td></tr> : sorted.map((r) => (
                <tr key={r.internal_order_id}>
                  <td className="mono">{fmtTime(r.created_at || '', tz, { withZone: true })}</td>
                  <td className="mono muted">{r.internal_order_id}</td>
                  <td className="mono muted">{r.broker_order_id ?? '—'}</td>
                  <td className="bold">{r.symbol}</td>
                  <td><span className={`trade-book-side ${r.side === 'BUY' ? 'buy' : 'sell'}`}>{r.side}</span></td>
                  <td className="mono">{fmtQty(r.quantity)}</td>
                  <td className="mono">{fmtQty(r.filled)}</td>
                  <td className="mono">{fmtQty(r.remaining)}</td>
                  <td>{r.order_type}</td>
                  <td className="mono">{r.limit_price != null ? fmtUsd(r.limit_price) : '—'}</td>
                  <td><span className={`trade-book-side ${statusClass(r.status)}`}>{r.status}</span></td>
                  <td className="mono">{r.avg_fill_price != null ? fmtUsd(r.avg_fill_price) : '—'}</td>
                  <td className="mono">{fmtTime(r.updated_at || '', tz, { withZone: true })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
          <button type="button" disabled={page<=1} onClick={() => setPage((p)=>Math.max(1,p-1))}>Prev</button>
          <span>Page {page} / {totalPages}</span>
          <button type="button" disabled={page>=totalPages} onClick={() => setPage((p)=>p+1)}>Next</button>
        </div>
      </section>
    </main>
  )
}
