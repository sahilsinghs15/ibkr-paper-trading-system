import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchReconcilePositions } from '../api/reconcileApi'
import { canFixDiff, FixDiffModal, fixTooltip } from '../components/FixDiffModal'
import { SortableTh } from '../components/SortableTh'
import type { FlattenBrokerPositionResponse, ReconcileDiffRow, ReconcilePositionsResponse } from '../types/reconcile'
import { normalizeIbkrAccount } from '../utils/activeAccount'
import { fmtCompactCurrency, fmtQty } from '../utils/format'
import { sortRows, useTableSortState } from '../utils/tableSort'

const DIFF_KIND_LABELS: Record<string, string> = {
  MATCH: 'Match',
  LEDGER_GHOST: 'Ledger ghost',
  BROKER_ORPHAN: 'Broker orphan',
  QTY_DRIFT: 'Qty drift',
  UNMAPPED_ACCOUNT: 'Unmapped account',
}

const DIFF_SORT_EXTRACTORS: Record<string, (row: ReconcileDiffRow) => unknown> = {
  kind: (row) => DIFF_KIND_LABELS[row.kind] ?? row.kind,
  symbol: (row) => row.symbol,
  sec_type: (row) => row.sec_type,
  broker_qty: (row) => row.broker_qty,
  ledger_qty: (row) => row.ledger_qty,
  in_flight: (row) => (row.in_flight ? 1 : 0),
}

function diffBadgeClass(kind: string): string {
  switch (kind) {
    case 'MATCH':
      return 'reconcile-badge match'
    case 'LEDGER_GHOST':
      return 'reconcile-badge ghost'
    case 'BROKER_ORPHAN':
      return 'reconcile-badge orphan'
    case 'QTY_DRIFT':
      return 'reconcile-badge drift'
    case 'UNMAPPED_ACCOUNT':
      return 'reconcile-badge unmapped'
    default:
      return 'reconcile-badge'
  }
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return 'Never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function brokerPriceKey(
  ibkrAccount: string | null | undefined,
  symbol: string,
  secType: string,
): string {
  return `${(ibkrAccount ?? '').trim().toUpperCase()}|${symbol.trim().toUpperCase()}|${secType.trim().toUpperCase()}`
}

function buildBrokerAvgCostMap(
  brokerPositions: ReconcilePositionsResponse['broker_positions'],
): Map<string, number> {
  const map = new Map<string, number>()
  for (const row of brokerPositions) {
    const key = brokerPriceKey(row.ibkr_account, row.symbol, row.sec_type)
    if (!map.has(key)) {
      map.set(key, row.avg_cost)
    }
  }
  return map
}

function fmtQtyWithNotional(
  qty: number | null | undefined,
  unitPrice: number | undefined,
): string {
  if (qty === null || qty === undefined) return '—'
  const qtyText = fmtQty(qty)
  if (unitPrice === undefined || unitPrice <= 0) return qtyText
  const notional = Math.abs(qty) * unitPrice
  return `${qtyText} / ${fmtCompactCurrency(notional)}`
}

export function ReconcilePage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount)
  const [data, setData] = useState<ReconcilePositionsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null)
  const [fixMessage, setFixMessage] = useState<string | null>(null)
  const [diffToFix, setDiffToFix] = useState<ReconcileDiffRow | null>(null)
  const { sortKey, sortDir, handleSort } = useTableSortState()

  const loadData = useCallback(async () => {
    try {
      setError(null)
      const res = await fetchReconcilePositions(cleanAccount || undefined)
      setData(res)
      setLastRefreshed(new Date())
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load reconcile data')
    } finally {
      setLoading(false)
    }
  }, [cleanAccount])

  useEffect(() => {
    loadData()
    const timer = setInterval(() => {
      void loadData()
    }, 30000)
    return () => clearInterval(timer)
  }, [loadData])

  const mismatchDiffs = useMemo(
    () => (data?.diffs ?? []).filter((d) => d.kind !== 'MATCH'),
    [data?.diffs],
  )

  const brokerAvgCostByKey = useMemo(
    () => buildBrokerAvgCostMap(data?.broker_positions ?? []),
    [data?.broker_positions],
  )

  const rawDiffs = useMemo(() => data?.diffs ?? [], [data?.diffs])
  const diffs = useMemo(() => {
    return sortRows(rawDiffs, sortKey, sortDir, DIFF_SORT_EXTRACTORS)
  }, [rawDiffs, sortKey, sortDir])

  const handleFixSuccess = useCallback(
    (res: FlattenBrokerPositionResponse) => {
      setFixMessage(
        res.success
          ? `Fix ${res.symbol}: ${res.status} (${res.side} ${res.quantity})`
          : `Fix ${res.symbol}: ${res.status} — ${res.message ?? 'Incomplete'}`,
      )
      void loadData()
    },
    [loadData],
  )

  if (loading && !data) {
    return (
      <main className="page reconcile-page">
        <div className="reconcile-loading">Loading reconcile diffs…</div>
      </main>
    )
  }

  if (error && !data) {
    return (
      <main className="page reconcile-page">
        <div className="status-badge off reconcile-error">
          RECONCILE UNAVAILABLE: {error}
        </div>
        <button type="button" className="reconcile-refresh-btn" onClick={() => void loadData()}>
          Retry
        </button>
      </main>
    )
  }

  const run = data?.run

  return (
    <main className="page reconcile-page">
      <header className="reconcile-header">
        <div className="reconcile-title-block">
          <h1>Inventory</h1>
          <span className="reconcile-subtitle">Account {cleanAccount || 'ALL'}</span>
        </div>
        <div className="reconcile-meta">
          <span>Last refreshed: {lastRefreshed ? lastRefreshed.toLocaleTimeString() : 'Never'}</span>
          <span>Last run: {fmtTime(run?.finished_at)}</span>
          <button type="button" className="reconcile-refresh-btn" onClick={() => void loadData()}>
            Refresh
          </button>
        </div>
      </header>

      {fixMessage ? (
        <div className="status-badge on reconcile-alert">{fixMessage}</div>
      ) : null}

      {run?.timed_out ? (
        <div className="status-badge idle reconcile-alert">
          Last IBKR snapshot timed out — ghost diffs suppressed; broker lines may be partial.
        </div>
      ) : null}

      {run?.error ? (
        <div className="status-badge off reconcile-alert">
          Last reconcile error: {run.error}
        </div>
      ) : null}

      <section className="reconcile-panel">
        <div className="reconcile-panel-head">
          <h2>
            Differences ({diffs.length} total · {mismatchDiffs.length} mismatches)
          </h2>
          <span className="reconcile-panel-hint">
            Per-row Fix aligns IBKR broker qty to the signal ledger
          </span>
        </div>
        <div className="reconcile-table-wrap">
          <table className="reconcile-table">
            <thead>
              <tr>
                <SortableTh sortKey="kind" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Kind</SortableTh>
                <SortableTh sortKey="symbol" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Symbol</SortableTh>
                <SortableTh sortKey="sec_type" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Sec type</SortableTh>
                <SortableTh sortKey="broker_qty" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Broker qty</SortableTh>
                <SortableTh sortKey="ledger_qty" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>Ledger qty</SortableTh>
                <SortableTh sortKey="in_flight" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort}>In flight</SortableTh>
                <th>Fix</th>
              </tr>
            </thead>
            <tbody>
              {diffs.length === 0 ? (
                <tr>
                  <td colSpan={7} className="reconcile-empty">
                    No diffs — reconciler may not have run yet.
                  </td>
                </tr>
              ) : (
                diffs.map((row, idx) => {
                  const enabled = canFixDiff(row, cleanAccount)
                  const priceKey = brokerPriceKey(row.ibkr_account, row.symbol, row.sec_type)
                  const unitPrice = brokerAvgCostByKey.get(priceKey)
                  return (
                    <tr key={`${row.kind}-${row.symbol}-${row.sec_type}-${idx}`}>
                      <td>
                        <span className={diffBadgeClass(row.kind)}>
                          {DIFF_KIND_LABELS[row.kind] ?? row.kind}
                        </span>
                      </td>
                      <td>{row.symbol}</td>
                      <td>{row.sec_type}</td>
                      <td className="mono">{fmtQtyWithNotional(row.broker_qty, unitPrice)}</td>
                      <td className="mono">{fmtQtyWithNotional(row.ledger_qty, unitPrice)}</td>
                      <td>{row.in_flight ? 'Yes' : '—'}</td>
                      <td>
                        <button
                          type="button"
                          className="reconcile-squareoff-btn"
                          disabled={!enabled}
                          title={fixTooltip(row, cleanAccount)}
                          onClick={() => setDiffToFix(row)}
                        >
                          Fix
                        </button>
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      {diffToFix ? (
        <FixDiffModal
          isOpen
          diff={diffToFix}
          ibkrAccount={(diffToFix.ibkr_account ?? cleanAccount).trim().toUpperCase()}
          ledgerPositions={data?.ledger_positions ?? []}
          onClose={() => setDiffToFix(null)}
          onSuccess={handleFixSuccess}
        />
      ) : null}
    </main>
  )
}
