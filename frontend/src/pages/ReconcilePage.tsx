import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchReconcilePositions } from '../api/reconcileApi'
import {
  FlattenDiffModal,
  canSquareOffDiff,
  squareOffTooltip,
} from '../components/FlattenDiffModal'
import type { FlattenBrokerPositionResponse, ReconcileDiffRow, ReconcilePositionsResponse } from '../types/reconcile'
import { normalizeIbkrAccount } from '../utils/activeAccount'

const DIFF_KIND_LABELS: Record<string, string> = {
  MATCH: 'Match',
  LEDGER_GHOST: 'Ledger ghost',
  BROKER_ORPHAN: 'Broker orphan',
  QTY_DRIFT: 'Qty drift',
  UNMAPPED_ACCOUNT: 'Unmapped account',
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

function fmtQty(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return Number.isInteger(value) ? String(value) : value.toFixed(4)
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return 'Never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export function ReconcilePage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount)
  const [data, setData] = useState<ReconcilePositionsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null)
  const [flattenMessage, setFlattenMessage] = useState<string | null>(null)
  const [diffToFlatten, setDiffToFlatten] = useState<ReconcileDiffRow | null>(null)

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

  const handleFlattenSuccess = useCallback(
    (res: FlattenBrokerPositionResponse) => {
      setFlattenMessage(
        res.success
          ? `Square off ${res.symbol}: ${res.status} (${res.side} ${res.quantity})`
          : `Square off ${res.symbol}: ${res.status} — ${res.message ?? 'Incomplete'}`,
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
  const diffs = data?.diffs ?? []

  return (
    <main className="page reconcile-page">
      <header className="reconcile-header">
        <div className="reconcile-title-block">
          <h1>Position Reconcile</h1>
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

      {flattenMessage ? (
        <div className="status-badge on reconcile-alert">{flattenMessage}</div>
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
            Per-row square off flattens IBKR broker line only
          </span>
        </div>
        <div className="reconcile-table-wrap">
          <table className="reconcile-table">
            <thead>
              <tr>
                <th>Kind</th>
                <th>Symbol</th>
                <th>Sec type</th>
                <th>Broker qty</th>
                <th>Ledger qty</th>
                <th>In flight</th>
                <th>Square off</th>
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
                  const enabled = canSquareOffDiff(row)
                  return (
                    <tr key={`${row.kind}-${row.symbol}-${row.sec_type}-${idx}`}>
                      <td>
                        <span className={diffBadgeClass(row.kind)}>
                          {DIFF_KIND_LABELS[row.kind] ?? row.kind}
                        </span>
                      </td>
                      <td>{row.symbol}</td>
                      <td>{row.sec_type}</td>
                      <td className="mono">{fmtQty(row.broker_qty)}</td>
                      <td className="mono">{fmtQty(row.ledger_qty)}</td>
                      <td>{row.in_flight ? 'Yes' : '—'}</td>
                      <td>
                        <button
                          type="button"
                          className="reconcile-squareoff-btn"
                          disabled={!enabled}
                          title={squareOffTooltip(row)}
                          onClick={() => setDiffToFlatten(row)}
                        >
                          Square off
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

      {diffToFlatten ? (
        <FlattenDiffModal
          isOpen
          diff={diffToFlatten}
          ibkrAccount={(diffToFlatten.ibkr_account ?? cleanAccount).trim().toUpperCase()}
          onClose={() => setDiffToFlatten(null)}
          onSuccess={handleFlattenSuccess}
        />
      ) : null}
    </main>
  )
}
