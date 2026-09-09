import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchPairDetail, patchPositionExits } from '../api/pairDetailApi'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import type { PositionLeg } from '../types/position'
import {
  fmtPnl,
  fmtQty,
  fmtTime,
  fmtUsd,
  num,
  pnlClass,
} from '../utils/format'

interface PairDetailModalProps {
  isOpen: boolean
  accountId: number
  tradeId: string
  legs?: PositionLeg[]
  onClose: () => void
}

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Request failed'
}

function thresholdInputValue(raw: string | number | null | undefined, unit: string): string {
  if (raw === null || raw === undefined || raw === '') return '0'
  const n = parseFloat(String(raw))
  if (Number.isNaN(n)) return '0'
  if (unit === 'PERCENT') return String(Math.round(n * 10000) / 100)
  return String(n)
}

function thresholdPayload(input: string, unit: string): string {
  const n = parseFloat(input)
  if (Number.isNaN(n) || n < 0) return '0'
  if (unit === 'PERCENT') return (n / 100).toFixed(6)
  return n.toFixed(4)
}

function resolveCurrency(magnitude: number, unit: string, notional: number): number | null {
  if (!magnitude || magnitude <= 0) return null
  if (unit === 'PERCENT') {
    if (!notional || notional <= 0) return null
    return magnitude * notional
  }
  return magnitude
}

function legGrossNotional(qty: unknown, mark: unknown): number | null {
  const q = num(qty)
  const p = num(mark)
  if (q === null || p === null) return null
  return Math.abs(q * p)
}

export function PairDetailModal({
  isOpen,
  accountId,
  tradeId,
  legs,
  onClose,
}: PairDetailModalProps) {
  const displayTz = usePnlStore((s) => s.displayTz)
  const active = usePnlStore((s) => s.active)
  const liveLegs = useMemo(() => {
    const grouped = groupLegs(active)
    const fromStore = grouped.get(`${accountId}|${tradeId}`)
    if (fromStore && fromStore.length) return fromStore
    return legs || []
  }, [active, accountId, tradeId, legs])
  const queryClient = useQueryClient()
  const queryKey = ['pair-detail', accountId, tradeId]

  const detailQuery = useQuery({
    queryKey,
    queryFn: () => fetchPairDetail(accountId, tradeId),
    enabled: isOpen && accountId > 0 && !!tradeId,
  })

  const [target, setTarget] = useState('0')
  const [stop, setStop] = useState('0')
  const [targetUnit, setTargetUnit] = useState('ABSOLUTE')
  const [stopUnit, setStopUnit] = useState('ABSOLUTE')
  const [armed, setArmed] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [savedMsg, setSavedMsg] = useState<string | null>(null)

  useEffect(() => {
    const exits = detailQuery.data?.exits
    if (!exits) return
    setTargetUnit(exits.target_unit || 'ABSOLUTE')
    setStopUnit(exits.stop_unit || 'ABSOLUTE')
    setTarget(thresholdInputValue(exits.target, exits.target_unit || 'ABSOLUTE'))
    setStop(thresholdInputValue(exits.stop, exits.stop_unit || 'ABSOLUTE'))
    setArmed(Boolean(exits.exit_automation_enabled))
    setFormError(null)
    setSavedMsg(null)
  }, [detailQuery.data])

  const mutation = useMutation({
    mutationFn: () =>
      patchPositionExits(accountId, tradeId, {
        target: thresholdPayload(target, targetUnit),
        stop: thresholdPayload(stop, stopUnit),
        target_unit: targetUnit,
        stop_unit: stopUnit,
        exit_automation_enabled: armed,
      }),
    onSuccess: async () => {
      setFormError(null)
      setSavedMsg('Exit settings saved. Monitor will use them on the next tick.')
      await queryClient.invalidateQueries({ queryKey })
    },
    onError: (err: unknown) => {
      setSavedMsg(null)
      setFormError(extractError(err))
    },
  })

  const head = liveLegs[0]
  const livePnl = head?.unrealized_pnl
  const pos = detailQuery.data?.position
  const pairText = pos
    ? pos.leg_b_symbol
      ? `${pos.leg_a_symbol} / ${pos.leg_b_symbol}`
      : pos.leg_a_symbol
    : liveLegs.length > 1
      ? `${liveLegs[0]?.symbol || '—'} / ${liveLegs[1]?.symbol || '—'}`
      : liveLegs[0]?.symbol || '—'

  const legANotional = pos
    ? legGrossNotional(pos.leg_a_signed_qty, pos.leg_a_entry_mark)
    : null
  const legBNotional = pos?.leg_b_symbol
    ? legGrossNotional(pos.leg_b_signed_qty, pos.leg_b_entry_mark)
    : null
  const pairNotional =
    num(pos?.entry_gross_notional) || (legANotional || 0) + (legBNotional || 0)
  const legsTotalNotional = (legANotional || 0) + (legBNotional || 0) || pairNotional || null
  const targetMag = parseFloat(thresholdPayload(target, targetUnit))
  const stopMag = parseFloat(thresholdPayload(stop, stopUnit))
  const targetAmt = resolveCurrency(targetMag, targetUnit, pairNotional)
  const stopAmt = resolveCurrency(stopMag, stopUnit, pairNotional)
  const liveNum = num(livePnl)
  const targetDistance =
    targetAmt !== null && liveNum !== null ? targetAmt - liveNum : null
  const stopDistance =
    stopAmt !== null && liveNum !== null ? liveNum - -stopAmt : null

  const orders = useMemo(
    () => [...(detailQuery.data?.orders || [])].sort((a, b) => a.id - b.id),
    [detailQuery.data],
  )
  const isOpenPair = (pos?.risk_state || 'OPEN') === 'OPEN'

  if (!isOpen) return null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-card signal-detail-modal pair-detail-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3>PAIR DETAIL — {pairText}</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body signal-detail-modal-body">
          <div className="pair-detail-meta">
            <div className="killswitch-account-badge">
              <span className="dim">ACCOUNT:</span>
              <span className="mono bold">{pos?.ibkr_account || head?.ibkr_account || '—'}</span>
            </div>
            <div className="killswitch-account-badge">
              <span className="dim">TRADE ID:</span>
              <span className="mono bold">{tradeId}</span>
            </div>
            <div className="killswitch-account-badge">
              <span className="dim">STATE:</span>
              <span className="mono bold">{pos?.risk_state || 'OPEN'}</span>
            </div>
            <div className="killswitch-account-badge">
              <span className="dim">LIVE PL:</span>
              <span className={`mono bold ${livePnl !== null && livePnl !== undefined ? pnlClass(livePnl) : ''}`}>
                {livePnl !== null && livePnl !== undefined ? fmtPnl(livePnl) : 'NO MARK'}
              </span>
            </div>
          </div>

          {detailQuery.isLoading ? <p className="dim">Loading pair history…</p> : null}
          {detailQuery.isError ? (
            <p className="settings-msg err">{extractError(detailQuery.error)}</p>
          ) : null}

          {pos ? (
            <section className="pair-detail-section">
              <h4>LEGS</h4>
              <table className="factory-table drawer-leg-table pair-legs-table">
                <thead>
                  <tr>
                    <th>LEG</th>
                    <th>SYMBOL</th>
                    <th>QTY</th>
                    <th>ENTRY</th>
                    <th>NOTIONAL</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>A</td>
                    <td className="mono">{pos.leg_a_symbol}</td>
                    <td className="mono">{fmtQty(pos.leg_a_signed_qty)}</td>
                    <td className="mono">{fmtUsd(pos.leg_a_entry_mark)}</td>
                    <td className="mono">{fmtUsd(legANotional)}</td>
                  </tr>
                  {pos.leg_b_symbol ? (
                    <tr>
                      <td>B</td>
                      <td className="mono">{pos.leg_b_symbol}</td>
                      <td className="mono">{fmtQty(pos.leg_b_signed_qty)}</td>
                      <td className="mono">{fmtUsd(pos.leg_b_entry_mark)}</td>
                      <td className="mono">{fmtUsd(legBNotional)}</td>
                    </tr>
                  ) : null}
                </tbody>
                <tfoot>
                  <tr>
                    <td colSpan={4}>TOTAL</td>
                    <td className="mono">{fmtUsd(legsTotalNotional)}</td>
                  </tr>
                </tfoot>
              </table>
              {pos.exit_reason ? (
                <p className="field-hint dim">Exit reason {pos.exit_reason}</p>
              ) : null}
            </section>
          ) : null}

          <section className="pair-detail-section">
            <div className="settings-block-h">
              <h4>EXIT AUTOMATION</h4>
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={armed}
                  disabled={!isOpenPair || mutation.isPending}
                  onChange={(e) => setArmed(e.target.checked)}
                />
                <span>{armed ? 'Armed for this pair' : 'Disarmed'}</span>
              </label>
            </div>
            {!isOpenPair ? (
              <p className="field-hint dim">Closed pairs cannot change stop or target.</p>
            ) : null}
            {detailQuery.data?.exits.monitor_enabled === false ? (
              <p className="settings-msg warn">
                Global risk-exit monitor is OFF. Saved values will not fire until
                RISK_EXIT_MONITOR_ENABLED is on.
              </p>
            ) : null}
            {detailQuery.data?.exits.shadow_mode ? (
              <p className="settings-msg warn">
                Shadow mode is on — the monitor will emit events without closing this pair.
              </p>
            ) : null}

            <div className="pair-exit-fields">
              <label className="field">
                <span>Pair target</span>
                <div className="money-field">
                  {targetUnit === 'ABSOLUTE' ? <span className="money-prefix">$</span> : null}
                  <input
                    type="number"
                    min="0"
                    step={targetUnit === 'PERCENT' ? '0.01' : '1'}
                    value={target}
                    disabled={!isOpenPair || mutation.isPending}
                    onChange={(e) => setTarget(e.target.value)}
                  />
                  <select
                    className="inline-input"
                    value={targetUnit}
                    disabled={!isOpenPair || mutation.isPending}
                    onChange={(e) => {
                      const next = e.target.value
                      setTarget(thresholdInputValue(thresholdPayload(target, targetUnit), next))
                      setTargetUnit(next)
                    }}
                  >
                    <option value="ABSOLUTE">USD</option>
                    <option value="PERCENT">% of pair</option>
                  </select>
                  {targetUnit === 'PERCENT' && targetAmt !== null ? (
                    <span className="money-suffix pair-pct-abs target">{fmtUsd(targetAmt)}</span>
                  ) : null}
                </div>
                <span className="field-hint dim">
                  {targetUnit === 'PERCENT'
                    ? targetAmt !== null
                      ? `${fmtUsd(targetAmt)} target · ${target}% of ${fmtUsd(pairNotional)}${targetDistance !== null ? ` · ${fmtPnl(targetDistance)} to target` : ''}`
                      : pairNotional <= 0 && parseFloat(target) > 0
                        ? 'Pair notional needed to convert %'
                        : '0 disables'
                    : targetAmt !== null
                      ? `Fires at ${fmtUsd(targetAmt)}${targetDistance !== null ? ` · ${fmtPnl(targetDistance)} to target` : ''}`
                      : '0 disables'}
                </span>
              </label>
              <label className="field">
                <span>Pair stop</span>
                <div className="money-field">
                  {stopUnit === 'ABSOLUTE' ? <span className="money-prefix">$</span> : null}
                  <input
                    type="number"
                    min="0"
                    step={stopUnit === 'PERCENT' ? '0.01' : '1'}
                    value={stop}
                    disabled={!isOpenPair || mutation.isPending}
                    onChange={(e) => setStop(e.target.value)}
                  />
                  <select
                    className="inline-input"
                    value={stopUnit}
                    disabled={!isOpenPair || mutation.isPending}
                    onChange={(e) => {
                      const next = e.target.value
                      setStop(thresholdInputValue(thresholdPayload(stop, stopUnit), next))
                      setStopUnit(next)
                    }}
                  >
                    <option value="ABSOLUTE">USD</option>
                    <option value="PERCENT">% of pair</option>
                  </select>
                  {stopUnit === 'PERCENT' && stopAmt !== null ? (
                    <span className="money-suffix pair-pct-abs stop">−{fmtUsd(stopAmt)}</span>
                  ) : null}
                </div>
                <span className="field-hint dim">
                  {stopUnit === 'PERCENT'
                    ? stopAmt !== null
                      ? `${fmtUsd(stopAmt)} stop · ${stop}% of ${fmtUsd(pairNotional)}${stopDistance !== null ? ` · ${fmtPnl(stopDistance)} of cushion` : ''}`
                      : pairNotional <= 0 && parseFloat(stop) > 0
                        ? 'Pair notional needed to convert %'
                        : '0 disables'
                    : stopAmt !== null
                      ? `Fires at −${fmtUsd(stopAmt)}${stopDistance !== null ? ` · ${fmtPnl(stopDistance)} of cushion` : ''}`
                      : '0 disables'}
                </span>
              </label>
            </div>
            {formError ? <p className="settings-msg err">{formError}</p> : null}
            {savedMsg ? <p className="settings-msg ok">{savedMsg}</p> : null}
            {isOpenPair ? (
              <div className="pair-exit-actions">
                <button
                  type="button"
                  className="btn primary"
                  disabled={mutation.isPending || detailQuery.isLoading}
                  onClick={() => mutation.mutate()}
                >
                  {mutation.isPending ? 'SAVING…' : 'SAVE STOP / TARGET'}
                </button>
              </div>
            ) : null}
          </section>

          <section className="pair-detail-section">
            <h4>ORDER HISTORY</h4>
            {orders.length === 0 ? (
              <p className="dim">No orders recorded for this trade.</p>
            ) : (
              <table className="factory-table drawer-leg-table pair-history-table">
                <thead>
                  <tr>
                    <th>TIME</th>
                    <th>LEG</th>
                    <th>SYMBOL</th>
                    <th>SIDE</th>
                    <th>QTY / FILL</th>
                    <th>PRICE</th>
                    <th>STATUS</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.map((ord) => (
                    <tr key={ord.id}>
                      <td className="mono">{fmtTime(ord.filled_at || ord.created_at, displayTz)}</td>
                      <td className="mono">
                        {ord.leg}
                        {ord.is_compensation ? ' · COMP' : ''}
                        {(ord.internal_order_id || '').includes(':RETRY:') ? ' · RETRY' : ''}
                      </td>
                      <td className="mono">{ord.symbol}</td>
                      <td className="mono">{ord.buy_sell}</td>
                      <td className="mono">
                        {fmtQty(ord.fill_qty)} / {fmtQty(ord.quantity)}
                      </td>
                      <td className="mono">{ord.fill_price != null ? fmtUsd(ord.fill_price) : '—'}</td>
                      <td className="mono">{ord.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {orders.some((o) => (o.executions || []).length > 0) ? (
              <div className="pair-fill-list dim-txt mono">
                <span className="fill-history-title">Fills</span>
                {orders.flatMap((ord) =>
                  (ord.executions || []).map((ex) => (
                    <div key={ex.id || ex.exec_id}>
                      {fmtTime(ex.executed_at, displayTz)} · {ex.symbol} {ex.side} +{ex.quantity} @{' '}
                      {fmtUsd(ex.price)}
                    </div>
                  )),
                )}
              </div>
            ) : null}
          </section>

          <section className="pair-detail-section">
            <h4>BASKETS</h4>
            {(detailQuery.data?.baskets || []).length === 0 ? (
              <p className="dim">No baskets recorded.</p>
            ) : (
              <ul className="pair-event-list">
                {(detailQuery.data?.baskets || []).map((b) => (
                  <li key={b.id} className="mono">
                    {b.action} · {b.state}
                    {b.recovery_status ? ` · ${b.recovery_status}` : ''}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="pair-detail-section">
            <h4>EVENTS</h4>
            {(detailQuery.data?.events || []).length === 0 ? (
              <p className="dim">No events recorded for this trade.</p>
            ) : (
              <ul className="pair-event-list">
                {(detailQuery.data?.events || []).map((ev) => (
                  <li key={ev.id}>
                    <span className="mono dim">{fmtTime(ev.ts, displayTz)}</span>{' '}
                    <span className="mono bold">{ev.kind}</span>
                    {ev.process ? <span className="dim"> · {ev.process}</span> : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}
