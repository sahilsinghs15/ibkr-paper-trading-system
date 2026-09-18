import { Fragment, useState } from 'react'
import { cancelManualOrder, type ManualOrderRead } from '../../api/manualTradingApi'
import { usePnlStore } from '../../store/pnlStore'
import { fmtQty, fmtTime } from '../../utils/format'
import { Pagination } from '../Pagination'
import { ACTIVE_ORDER_STATUSES, apiErrorMessage, ORDERS_PAGE_SIZE } from './useManualWorkspace'

interface Props {
  account: string
  orders: ManualOrderRead[]
  total: number
  page: number
  setPage: (page: number) => void
  loading: boolean
  loaded: boolean
  error: string | null
  onRetry: () => void
  /** Called after a cancel request so orders and positions refresh together. */
  onCancelled: () => void
}

function statusBadge(s: string) {
  if (s === 'FILLED') return <span className="badge b-filled">Filled</span>
  if (s === 'PARTIALLY_FILLED') return <span className="badge b-partial">Partial</span>
  if (s === 'CANCELLED') return <span className="badge b-closed">Cancelled</span>
  if (s === 'REJECTED' || s === 'ERROR') return <span className="badge b-rej">{s === 'ERROR' ? 'Error' : 'Rejected'}</span>
  if (s === 'PENDING_SUBMIT') return <span className="badge">Pending</span>
  return <span className="badge b-exec">Working</span>
}

function remainingOf(o: ManualOrderRead): string {
  const r = Number(o.quantity) - Number(o.filled_quantity ?? 0)
  return Number.isFinite(r) ? fmtQty(r) : '—'
}

/** Trim trailing zeros from decimal strings ("250.50000000" → "250.5"). */
function plainNumber(v: unknown): string {
  const n = Number(v)
  return Number.isFinite(n) ? String(n) : String(v ?? '—')
}

export function ManualOrdersBlotter({
  account,
  orders,
  total,
  page,
  setPage,
  loading,
  loaded,
  error,
  onRetry,
  onCancelled,
}: Props) {
  const displayTz = usePnlStore((s) => s.displayTz)
  const [expandedOrderId, setExpandedOrderId] = useState<number | null>(null)
  const [cancelConfirm, setCancelConfirm] = useState<ManualOrderRead | null>(null)
  const [cancellingOrderId, setCancellingOrderId] = useState<number | null>(null)
  const [cancelFeedback, setCancelFeedback] = useState<{ message: string; isError: boolean } | null>(null)
  const working = orders.filter((o) => ACTIVE_ORDER_STATUSES.includes(o.status)).length

  const handleCancel = async (order: ManualOrderRead) => {
    if (!account || cancellingOrderId !== null) return
    setCancellingOrderId(order.id)
    setCancelConfirm(null)
    setCancelFeedback(null)
    try {
      // Authoritative, audited backend path (POST /manual/orders/{id}/cancel).
      const res = await cancelManualOrder(account, order.id)
      setCancelFeedback({ message: res.message || 'Cancellation sent.', isError: !res.success })
    } catch (err: unknown) {
      setCancelFeedback({ message: apiErrorMessage(err, 'Cancel failed'), isError: true })
    } finally {
      setCancellingOrderId(null)
      onCancelled()
    }
  }

  return (
    <div className="mw-subsection" aria-labelledby="mw-blotter-title">
      <div className="mw-subsection-head">
        <h3 id="mw-blotter-title">
          Order blotter <span className="mw-count">{loaded ? total : '—'}</span>
          {working > 0 && <span className="mw-live-pill">{working} working · auto-refresh</span>}
        </h3>
        <div className="mw-section-actions">
          {loading && loaded && <span className="mw-hint">Updating…</span>}
        </div>
      </div>

      {cancelFeedback && (
        <div className={`mw-note ${cancelFeedback.isError ? 'error' : 'ok'}`}>{cancelFeedback.message}</div>
      )}
      {error && (
        <div className="mw-note error">
          {loaded ? 'Showing last loaded orders — refresh failed: ' : ''}
          {error}{' '}
          <button type="button" className="manual-btn" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}

      <div className="manual-table-wrap mw-table-wrap">
        <table className="manual-table mw-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Symbol</th>
              <th>Side</th>
              <th className="num">Qty</th>
              <th className="num">Filled</th>
              <th className="num">Remaining</th>
              <th>Type</th>
              <th className="num">Price</th>
              <th>Status</th>
              <th className="num">Action</th>
            </tr>
          </thead>
          <tbody>
            {!loaded ? (
              <tr>
                <td colSpan={10} className="mw-empty">
                  {error ? 'Orders could not be loaded.' : 'Loading manual orders…'}
                </td>
              </tr>
            ) : orders.length === 0 ? (
              <tr>
                <td colSpan={10} className="mw-empty">
                  No manual orders for {account || '—'} yet. Orders placed with the ticket above appear here.
                </td>
              </tr>
            ) : (
              orders.map((o) => {
                const isCancellable = ACTIVE_ORDER_STATUSES.includes(o.status)
                const filled = String(o.filled_quantity ?? 0)
                const expanded = expandedOrderId === o.id
                return (
                  <Fragment key={o.id}>
                    <tr className={expanded ? 'is-expanded' : ''}>
                      <td className="mono dim">{fmtTime(o.created_at, displayTz)}</td>
                      <td className="mono strong">{o.symbol}</td>
                      <td>
                        <span className={o.side === 'BUY' ? 'side-buy' : 'side-sell'}>{o.side}</span>
                      </td>
                      <td className="mono num">{fmtQty(o.quantity)}</td>
                      <td className={`mono num ${Number(filled) > 0 ? '' : 'dim'}`}>{fmtQty(filled)}</td>
                      <td className="mono num dim">{remainingOf(o)}</td>
                      <td>{o.order_type}</td>
                      <td className="mono num">{o.limit_price ? plainNumber(o.limit_price) : 'MKT'}</td>
                      <td>{statusBadge(o.status)}</td>
                      <td className="num">
                        <div className="mw-row-actions">
                          <button
                            type="button"
                            className="manual-btn mw-row-btn"
                            onClick={() => setExpandedOrderId(expanded ? null : o.id)}
                            aria-expanded={expanded}
                          >
                            {expanded ? 'Hide' : 'Details'}
                          </button>
                          {isCancellable && (
                            <button
                              type="button"
                              className="manual-btn manual-btn-danger mw-row-btn"
                              disabled={cancellingOrderId !== null}
                              onClick={() => setCancelConfirm(o)}
                            >
                              {cancellingOrderId === o.id ? '…' : 'Cancel'}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                    {expanded && (
                      <tr className="mw-detail-row">
                        <td colSpan={10}>
                          <dl className="mw-detail-grid">
                            <div>
                              <dt>Order</dt>
                              <dd className="mono">{o.internal_order_id}</dd>
                            </div>
                            <div>
                              <dt>Trade</dt>
                              <dd className="mono">{o.trade_id}</dd>
                            </div>
                            <div>
                              <dt>Broker order</dt>
                              <dd className="mono">
                                {o.broker_order_id ?? '—'} · perm {o.perm_id ?? '—'}
                              </dd>
                            </div>
                            <div>
                              <dt>Contract</dt>
                              <dd>
                                <span className="mono">{o.con_id}</span> {o.exchange} · {o.currency}
                              </dd>
                            </div>
                            <div>
                              <dt>Time in force</dt>
                              <dd className="mono">
                                {o.tif} · {o.order_type}
                              </dd>
                            </div>
                            <div>
                              <dt>Submitted</dt>
                              <dd className="mono">{fmtTime(o.submitted_at, displayTz)}</dd>
                            </div>
                          </dl>
                          {o.reject_reason && <div className="mw-note error">{o.reject_reason}</div>}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      {total > ORDERS_PAGE_SIZE && (
        <div className="mw-footer">
          <Pagination currentPage={page} totalItems={total} pageSize={ORDERS_PAGE_SIZE} onPageChange={setPage} />
        </div>
      )}

      {cancelConfirm && (
        <div className="manual-modal-backdrop" onClick={() => setCancelConfirm(null)} role="presentation">
          <div
            className="manual-modal"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-labelledby="cancel-title"
          >
            <div className="manual-modal-head">
              <h2 id="cancel-title">Cancel order?</h2>
              <button type="button" className="manual-btn" onClick={() => setCancelConfirm(null)}>
                Close
              </button>
            </div>
            <div className="manual-modal-body">
              <div style={{ fontSize: 12 }}>
                <div>
                  <span className="dim">Order</span> <strong className="mono">{cancelConfirm.internal_order_id}</strong> ·{' '}
                  <span className={cancelConfirm.side === 'BUY' ? 'side-buy' : 'side-sell'}>{cancelConfirm.side}</span>{' '}
                  {fmtQty(cancelConfirm.quantity)} {cancelConfirm.symbol}
                </div>
                <div style={{ marginTop: 6, fontSize: 11, color: 'var(--muted)' }}>
                  Broker {cancelConfirm.broker_order_id ?? '—'} · Status {cancelConfirm.status} · Filled{' '}
                  {String(cancelConfirm.filled_quantity ?? 0)} · Remaining {remainingOf(cancelConfirm)}
                </div>
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <button type="button" className="manual-btn" onClick={() => setCancelConfirm(null)}>
                  Keep order
                </button>
                <button
                  type="button"
                  className="manual-btn manual-btn-danger"
                  onClick={() => void handleCancel(cancelConfirm)}
                  disabled={cancellingOrderId !== null}
                >
                  Cancel order
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
