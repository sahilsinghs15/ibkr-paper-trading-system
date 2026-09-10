import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchTradeBook } from '../api/brokerExecutionsApi'
import { SortableTh } from '../components/SortableTh'
import type { BrokerExecutionLine, TradeBookPaginatedResponse } from '../types/brokerExecution'
import { normalizeIbkrAccount } from '../utils/activeAccount'
import { displayInstrument, fmtQty, fmtTime, fmtUsd, loadTimezone } from '../utils/format'
import { sortRows, useTableSortState } from '../utils/tableSort'

const EXECUTION_SORT_EXTRACTORS: Record<string, (row: BrokerExecutionLine) => unknown> = {
  time: (row) => {
    const t = new Date(row.executed_at).getTime()
    return Number.isNaN(t) ? row.executed_at : t
  },
  symbol: (row) => row.symbol,
  type: (row) => displayInstrument(row.sec_type),
  side: (row) => row.side,
  qty: (row) => row.quantity,
  price: (row) => row.price,
  notional: (row) => row.quantity * row.price,
  commission: (row) => row.commission,
  exec_id: (row) => row.exec_id,
  broker_order_id: (row) => row.broker_order_id,
  order_status: (row) => row.order_status || '',
}

function defaultSort(a: BrokerExecutionLine, b: BrokerExecutionLine): number {
  const timeA = new Date(a.executed_at).getTime()
  const timeB = new Date(b.executed_at).getTime()
  if (!Number.isNaN(timeA) && !Number.isNaN(timeB)) {
    return timeB - timeA
  }
  return b.executed_at.localeCompare(a.executed_at)
}

export function TradeBookPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount)
  const [data, setData] = useState<TradeBookPaginatedResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [isGatewayDown, setIsGatewayDown] = useState(false)
  const [page, setPage] = useState(1)
  const pageSize = 50
  const { sortKey, sortDir, handleSort } = useTableSortState()
  const tz = loadTimezone()

  const loadData = useCallback(async () => {
    if (!cleanAccount) {
      setError('No IBKR account specified.')
      setLoading(false)
      return
    }
    try {
      setLoading(true)
      setError(null)
      setIsGatewayDown(false)
      const res = await fetchTradeBook(cleanAccount, { page, page_size: pageSize })
      setData(res)
    } catch (err: unknown) {
      const axiosError = err as { response?: { status?: number; data?: { detail?: string } }; message?: string }
      if (axiosError?.response?.status === 503) {
        setIsGatewayDown(true)
        setError('TWS gateway is down.')
      } else if (axiosError?.response?.status === 403) {
        setError('Forbidden: Cannot access another account.')
      } else {
        const detail = axiosError?.response?.data?.detail
        setError(detail || (err instanceof Error ? err.message : 'Failed to load execution data'))
      }
    } finally {
      setLoading(false)
    }
  }, [cleanAccount, page])

  useEffect(() => {
    let mounted = true
    if (cleanAccount) {
      void fetchTradeBook(cleanAccount, { page, page_size: pageSize }).then(
        (res) => {
          if (mounted) {
            setData(res)
            setLoading(false)
          }
        },
        (err: unknown) => {
          if (mounted) {
            const axiosError = err as { response?: { status?: number; data?: { detail?: string } }; message?: string }
            if (axiosError?.response?.status === 503) {
              setIsGatewayDown(true)
              setError('TWS gateway is down.')
            } else if (axiosError?.response?.status === 403) {
              setError('Forbidden: Cannot access another account.')
            } else {
              const detail = axiosError?.response?.data?.detail
              setError(detail || (err instanceof Error ? err.message : 'Failed to load execution data'))
            }
            setLoading(false)
          }
        },
      )
    }
    return () => {
      mounted = false
    }
  }, [cleanAccount, page])

  const sortedExecutions = useMemo(() => {
    const list = data?.executions ?? []
    return sortRows(list, sortKey, sortDir, EXECUTION_SORT_EXTRACTORS, defaultSort)
  }, [data?.executions, sortKey, sortDir])

  if (loading && !data) {
    return (
      <main className="page trade-book-page">
        <header className="trade-book-header">
          <div className="trade-book-title-block">
            <h1>Trade Book</h1>
            <span className="trade-book-subtitle">IBKR executions since midnight (Gateway) · {cleanAccount}</span>
          </div>
        </header>
        <div className="trade-book-loading">Loading IBKR executions…</div>
      </main>
    )
  }

  if (error && !data) {
    return (
      <main className="page trade-book-page">
        <header className="trade-book-header">
          <div className="trade-book-title-block">
            <h1>Trade Book</h1>
            <span className="trade-book-subtitle">IBKR executions since midnight (Gateway) · {cleanAccount}</span>
          </div>
        </header>
        <div className="status-badge off trade-book-alert">
          {isGatewayDown ? 'GATEWAY DOWN: TWS gateway is down.' : error}
        </div>
        <button
          type="button"
          className="trade-book-refresh-btn"
          onClick={() => void loadData()}
        >
          Retry
        </button>
      </main>
    )
  }

  const totalPages = data ? Math.ceil(data.total / pageSize) : 1
  const lastSyncedText = data?.last_synced_at ? fmtTime(data.last_synced_at, tz, { withZone: true }) : '—'
  return (
    <main className="page trade-book-page">
      <header className="trade-book-header">
        <div className="trade-book-title-block">
          <h1>Trade Book</h1>
          <span className="trade-book-subtitle">Persisted Trade Book · {cleanAccount} · Last synced: {lastSyncedText}</span>
        </div>
        <div className="trade-book-meta">
          <button
            type="button"
            className="trade-book-refresh-btn"
            disabled={loading}
            onClick={() => void loadData()}
          >
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      {isGatewayDown ? (
        <div className="status-badge off trade-book-alert">
          GATEWAY DOWN: TWS gateway is down.
        </div>
      ) : null}

      {error && !isGatewayDown ? (
        <div className="status-badge off trade-book-alert">
          {error}
        </div>
      ) : null}

      {data?.timed_out ? (
        <div className="status-badge idle trade-book-alert">
          Gateway execution request timed out; executions may be partial.
        </div>
      ) : null}

      <section className="trade-book-panel">
        <div className="trade-book-panel-head">
          <h2>Executions ({data?.total ?? sortedExecutions.length})</h2>
          <span className="trade-book-panel-hint">Persisted Trade Book · page {page}/{totalPages}</span>
        </div>

        <div className="trade-book-table-wrap">
          <table className="trade-book-table">
            <thead>
              <tr>
                <SortableTh sortKey="time" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Time</SortableTh>
                <SortableTh sortKey="symbol" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Symbol</SortableTh>
                <SortableTh sortKey="type" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Type</SortableTh>
                <SortableTh sortKey="side" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Side</SortableTh>
                <SortableTh sortKey="qty" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Qty</SortableTh>
                <SortableTh sortKey="price" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Price</SortableTh>
                <SortableTh sortKey="notional" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Notional</SortableTh>
                <SortableTh sortKey="commission" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Commission</SortableTh>
                <SortableTh sortKey="exec_id" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Exec ID</SortableTh>
                <SortableTh sortKey="broker_order_id" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Broker Order ID</SortableTh>
                <SortableTh sortKey="order_status" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Order Status</SortableTh>
              </tr>
            </thead>
            <tbody>
              {sortedExecutions.length === 0 ? (
                <tr>
                  <td colSpan={11} className="trade-book-empty">
                    No executions
                  </td>
                </tr>
              ) : (
                sortedExecutions.map((row) => {
                  const notional = row.quantity * row.price
                  const commText = row.commission != null
                    ? `${fmtUsd(row.commission)} ${row.commission_currency || ''}`.trim()
                    : '—'
                  const status = row.order_status
                  return (
                    <tr key={row.exec_id}>
                      <td className="mono">{fmtTime(row.executed_at, tz, { withZone: true })}</td>
                      <td className="bold">{row.symbol}</td>
                      <td>{displayInstrument(row.sec_type)}</td>
                      <td>
                        <span className={`trade-book-side ${row.side === 'BUY' ? 'buy' : 'sell'}`}>
                          {row.side}
                        </span>
                      </td>
                      <td className="mono">{fmtQty(row.quantity)}</td>
                      <td className="mono">{fmtUsd(row.price)}</td>
                      <td className="mono">{fmtUsd(notional)}</td>
                      <td className="mono">{commText}</td>
                      <td className="mono muted">{row.exec_id}</td>
                      <td className="mono muted">{row.broker_order_id ?? '—'}</td>
                      <td>{status ? <span className={`trade-book-side ${status === 'FILLED' ? 'buy' : status === 'REJECTED' || status === 'ERROR' ? 'sell' : ''}`}>{status}</span> : '—'}</td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
          <button type="button" disabled={page<=1} onClick={() => setPage((p)=>Math.max(1,p-1))}>Prev</button>
          <span>Page {page} / {totalPages} · Total {data?.total ?? 0}</span>
          <button type="button" disabled={page>=totalPages} onClick={() => setPage((p)=>p+1)}>Next</button>
        </div>
      </section>
    </main>
  )
}
